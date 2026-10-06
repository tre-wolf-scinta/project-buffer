"""Turns a stored original into a sanitized analysis. Runs in the worker."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from project_buffer.domain.analysis import MessageAnalysisResult
from project_buffer.domain.enums import (
    Confidence,
    Direction,
    JobKind,
    NotificationKind,
    ProcessingStatus,
    Urgency,
)
from project_buffer.infrastructure.db.models import Message, MessageAnalysis
from project_buffer.infrastructure.llm.base import AnalysisRequest, LLMInvalidOutputError
from project_buffer.infrastructure.llm.prompts import PROMPT_VERSION
from project_buffer.services import audit, guards, jobs
from project_buffer.services.container import Services

logger = logging.getLogger(__name__)

SAFETY_NOTE = (
    "Urgency was raised automatically because the original message contains "
    "safety-related terms. Review the details."
)


def decrypt_body(services: Services, message: Message) -> str:
    return services.crypto.decrypt(message.body_ciphertext, aad=message.body_aad()).decode()


def analyze_message(
    session: Session, services: Services, message_id: uuid.UUID, now: datetime
) -> None:
    """Analyze one inbound message and store the result. Safe to run more than once."""
    message = session.get(Message, message_id)
    if message is None or message.direction != Direction.INBOUND:
        return
    if message.processing_status == ProcessingStatus.QUARANTINED:
        return

    settings = services.settings
    body = decrypt_body(services, message)
    attachment_types = [a.content_type for a in message.attachments]

    if not body.strip():
        result = _empty_message_result(len(attachment_types))
        provider, model = "none", "none"
    else:
        request = AnalysisRequest(
            message_text=body,
            sender_name=settings.coparent_display_name,
            recipient_name=settings.owner_display_name,
            received_at_local=message.occurred_at.astimezone(settings.timezone).strftime(
                "%A %Y-%m-%d %H:%M %Z"
            ),
            children_names=settings.children,
            attachment_types=attachment_types,
        )
        result = _guarded_analysis(services, request, body)
        provider, model = services.llm.name, services.llm.model

    keywords = guards.safety_keywords_present(body)
    result = apply_safety_bias(result, keywords)

    for previous in message.analyses:
        previous.is_current = False
    session.add(
        MessageAnalysis(
            message_id=message.id,
            llm_provider=provider,
            llm_model=model,
            prompt_version=PROMPT_VERSION,
            short_summary=result.short_summary,
            topic=result.topic,
            structured=result.model_dump(mode="json"),
            search_text="\n".join(result.text_fields()),
            safety_keywords_detected=keywords,
        )
    )
    message.processing_status = ProcessingStatus.PROCESSED
    message.processed_at = now
    if not message.urgency_overridden:
        message.urgency = result.urgency
    if message.handled_at is None:
        message.requires_response = result.response_needed

    jobs.enqueue(
        session,
        JobKind.NOTIFY_OWNER,
        now=now,
        max_attempts=settings.job_max_attempts,
        message_id=message.id,
        payload={"kind": NotificationKind.SUMMARY.value},
        dedupe_key=f"notify:summary:{message.id}",
    )
    audit.record(
        session,
        actor="worker",
        action="message_analyzed",
        subject_type="message",
        subject_id=message.id,
        llm_provider=provider,
        llm_model=model,
        urgency=result.urgency.value,
    )
    logger.info("message analyzed id=%s urgency=%s", message.id, result.urgency.value)


def _guarded_analysis(
    services: Services, request: AnalysisRequest, original: str
) -> MessageAnalysisResult:
    """Call the model; if a guard rejects the output, try once more with a hint."""
    result = services.llm.analyze_message(request)
    try:
        guards.check_sanitized_output(original, result.text_fields())
        return result
    except guards.GuardViolation as violation:
        logger.warning("analysis rejected by guard; retrying with hint")
        retry = dataclasses.replace(request, retry_hint=violation.reason)
    result = services.llm.analyze_message(retry)
    try:
        guards.check_sanitized_output(original, result.text_fields())
    except guards.GuardViolation:
        raise LLMInvalidOutputError("output failed sanitization guard twice") from None
    return result


def apply_safety_bias(result: MessageAnalysisResult, keywords: bool) -> MessageAnalysisResult:
    """Never let a possible safety matter sit below 'urgent'."""
    if result.urgency.is_elevated or not (keywords or result.safety_issue):
        return result
    notes = list(result.ambiguity_notes)
    if keywords and SAFETY_NOTE not in notes:
        notes = [SAFETY_NOTE, *notes][:12]
    return result.model_copy(update={"urgency": Urgency.URGENT, "ambiguity_notes": notes})


def _empty_message_result(num_attachments: int) -> MessageAnalysisResult:
    if not num_attachments:
        return MessageAnalysisResult.minimal(
            short_summary="The message was empty.",
            topic="Empty message",
            urgency=Urgency.INFORMATIONAL,
        )
    noun = "attachment" if num_attachments == 1 else "attachments"
    return MessageAnalysisResult.minimal(
        short_summary=f"The message has no text and {num_attachments} {noun}.",
        topic="Attachment only",
        attachments_description=f"{num_attachments} {noun}, not analyzed automatically.",
        confidence=Confidence.LOW,
        ambiguity_notes=[
            "Attachments are not analyzed automatically and may contain unfiltered content."
        ],
    )


def mark_analysis_failed(
    session: Session, services: Services, message_id: uuid.UUID, now: datetime
) -> None:
    """Called when the analysis job has exhausted its retries."""
    message = session.get(Message, message_id)
    if message is None or message.processing_status == ProcessingStatus.PROCESSED:
        return
    message.processing_status = ProcessingStatus.FAILED
    jobs.enqueue(
        session,
        JobKind.NOTIFY_OWNER,
        now=now,
        max_attempts=services.settings.job_max_attempts,
        message_id=message.id,
        payload={"kind": NotificationKind.PROCESSING_FAILED.value},
        dedupe_key=f"notify:processing_failed:{message.id}",
    )
    audit.record(
        session,
        actor="worker",
        action="message_analysis_failed",
        subject_type="message",
        subject_id=message.id,
    )


def request_reprocess(
    session: Session, services: Services, message: Message, now: datetime, *, actor: str = "owner"
) -> bool:
    """Queue a fresh analysis. Also releases a quarantined message. Returns False if one is
    already queued."""
    if message.direction != Direction.INBOUND:
        return False
    if jobs.has_active_job(session, JobKind.ANALYZE_MESSAGE, message.id):
        return False
    was_quarantined = message.processing_status == ProcessingStatus.QUARANTINED
    message.processing_status = ProcessingStatus.PENDING
    jobs.enqueue(
        session,
        JobKind.ANALYZE_MESSAGE,
        now=now,
        max_attempts=services.settings.job_max_attempts,
        message_id=message.id,
    )
    if was_quarantined:
        from project_buffer.services.ingestion import enqueue_media_jobs

        enqueue_media_jobs(
            session,
            list(message.attachments),
            now=now,
            max_attempts=services.settings.job_max_attempts,
        )
    audit.record(
        session,
        actor=actor,
        action="message_released" if was_quarantined else "message_reprocess_requested",
        subject_type="message",
        subject_id=message.id,
    )
    return True
