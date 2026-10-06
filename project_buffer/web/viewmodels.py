"""Shapes handed to templates. Built only from sanitized data.

Nothing here reads ``Message.body_ciphertext`` for inbound messages, so list
and detail pages cannot leak original text by accident.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from project_buffer.config import Settings
from project_buffer.domain.analysis import MessageAnalysisResult
from project_buffer.domain.enums import DeliveryStatus, ProcessingStatus, Urgency
from project_buffer.domain.phone import mask_phone
from project_buffer.infrastructure.db.models import Message

URGENCY_LABELS = {
    Urgency.EMERGENCY: "Emergency",
    Urgency.URGENT: "Urgent",
    Urgency.NORMAL: "Normal",
    Urgency.INFORMATIONAL: "Informational",
}

DELIVERY_LABELS = {
    DeliveryStatus.SENDING: "Sending",
    DeliveryStatus.QUEUED: "Accepted by the carrier network, not yet delivered",
    DeliveryStatus.SENT: "Sent, delivery not yet confirmed",
    DeliveryStatus.DELIVERED: "Delivered",
    DeliveryStatus.UNDELIVERED: "Not delivered",
    DeliveryStatus.FAILED: "Failed, not sent",
    DeliveryStatus.UNCERTAIN: "Unconfirmed. Checking whether it was sent. Do not resend yet",
    DeliveryStatus.NOT_APPLICABLE: "",
}

FLAG_LABELS = {
    "safety_issue": "Safety",
    "medical_issue": "Medical",
    "school_issue": "School",
    "schedule_issue": "Schedule",
    "financial_issue": "Financial",
    "legal_or_custody_issue": "Legal or custody",
}


def format_when(value: datetime, tz: ZoneInfo, now: datetime) -> str:
    """'Today 7:42 PM', 'Tuesday 7:42 PM', or 'Oct 6, 2026, 7:42 PM'."""
    local, local_now = value.astimezone(tz), now.astimezone(tz)
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == local_now.date():
        return f"Today {clock}"
    if local.date() == (local_now - timedelta(days=1)).date():
        return f"Yesterday {clock}"
    if timedelta(0) <= local_now - local < timedelta(days=6):
        return f"{local.strftime('%A')} {clock}"
    return f"{local.strftime('%b')} {local.day}, {local.year}, {clock}"


@dataclass
class MessageCard:
    id: uuid.UUID
    sender: str
    when: str
    when_iso: str
    topic: str
    state: str  # processed | pending | failed | quarantined | outbound
    urgency: Urgency = Urgency.NORMAL
    urgency_label: str = ""
    summary: str = ""
    requests: list[str] = field(default_factory=list)
    information: list[str] = field(default_factory=list)
    response_needed: bool = False
    omitted: bool = False
    handled: bool = False
    unread: bool = False
    delivery_label: str = ""
    num_attachments: int = 0

    @property
    def elevated(self) -> bool:
        return self.urgency.is_elevated

    @property
    def heading(self) -> str:
        prefix = f"{self.urgency_label}: " if self.elevated else ""
        return f"{prefix}{self.topic}"

    @property
    def context(self) -> str:
        """Phrase that makes link and button names unique, e.g. 'about X from Y'."""
        if self.state == "outbound":
            return f"sent {self.when}"
        return f"about {self.topic} from {self.sender}, {self.when}"


def analysis_of(message: Message) -> MessageAnalysisResult | None:
    current = message.current_analysis
    return MessageAnalysisResult.model_validate(current.structured) if current else None


def build_card(message: Message, settings: Settings, now: datetime) -> MessageCard:
    when = format_when(message.occurred_at, settings.timezone, now)
    base: dict[str, Any] = {
        "id": message.id,
        "when": when,
        "when_iso": message.occurred_at.isoformat(),
        "handled": message.handled_at is not None,
        "num_attachments": message.num_media,
    }
    if not message.is_inbound:
        return MessageCard(
            **base,
            sender="You",
            topic=f"Message to {settings.coparent_display_name}",
            state="outbound",
            delivery_label=DELIVERY_LABELS[message.delivery_status],
        )
    unread = message.read_at is None
    if message.processing_status == ProcessingStatus.QUARANTINED:
        return MessageCard(
            **base,
            sender=f"Unrecognized {mask_phone(message.from_number)}",
            topic="Unprocessed message from an unrecognized number",
            state="quarantined",
            unread=unread,
        )
    sender = settings.coparent_display_name
    result = analysis_of(message)
    if result is None or message.processing_status != ProcessingStatus.PROCESSED:
        failed = message.processing_status == ProcessingStatus.FAILED
        return MessageCard(
            **base,
            sender=sender,
            topic="Filtering failed" if failed else "Filtering in progress",
            state="failed" if failed else "pending",
            unread=unread,
            summary=(
                "A message was received, but automatic filtering failed. The original is "
                "saved and is not shown."
                if failed
                else "A message was received and is being filtered."
            ),
        )
    return MessageCard(
        **base,
        sender=sender,
        topic=result.topic,
        state="processed",
        urgency=message.urgency,
        urgency_label=URGENCY_LABELS[message.urgency],
        summary=result.short_summary,
        requests=result.requests,
        information=(
            result.factual_information + result.sender_statements + result.sender_positions
        )[:6],
        response_needed=message.requires_response,
        omitted=result.omitted_content_present,
        unread=unread,
    )


@dataclass
class DetailSection:
    title: str
    items: list[str]
    note: str = ""


def detail_sections(result: MessageAnalysisResult) -> list[DetailSection]:
    """Non-empty parts of an analysis, in reading order."""
    times = [f"{m.what}: {m.when}" for m in result.dates_and_times]
    candidates = [
        DetailSection("Requests", result.requests),
        DetailSection("Questions", result.questions),
        DetailSection("Decisions needed", result.decisions_needed),
        DetailSection("Dates and times", times),
        DetailSection("Locations", list(result.locations)),
        DetailSection("Information", result.factual_information),
        DetailSection(
            "Statements by the sender",
            result.sender_statements,
            "Reported by the sender. Not verified.",
        ),
        DetailSection("Sender's positions", result.sender_positions),
        DetailSection(
            "Allegations",
            result.allegations_relevant_to_parenting_or_legal_matters,
            "These are the sender's claims. They are not established facts.",
        ),
        DetailSection("Commitments made by the sender", result.commitments_made_by_sender),
        DetailSection("Children involved", list(result.children_involved)),
        DetailSection("Unclear or uncertain", result.ambiguity_notes),
    ]
    return [section for section in candidates if section.items]


def flag_labels(result: MessageAnalysisResult) -> list[str]:
    return [label for name, label in FLAG_LABELS.items() if getattr(result, name)]


OMITTED_LABELS = {
    "insults": "insults",
    "profanity": "profanity",
    "character_attacks": "attacks on character",
    "parenting_attacks": "attacks on parenting",
    "attacks_on_partner": "attacks on a partner or family",
    "guilt_or_shaming": "guilt or shaming",
    "repeated_accusations": "repeated accusations",
    "rhetorical_attacks": "rhetorical attacks",
    "ill_wishes": "ill wishes",
    "inflammatory_filler": "inflammatory filler",
    "embedded_instructions": "text addressed to an automated system",
    "other": "other personal commentary",
}
