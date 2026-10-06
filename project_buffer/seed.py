"""Development-only demo data.

Every name, number and message here is invented. Never put real family
communications in this file.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pyotp
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from project_buffer.domain.analysis import MessageAnalysisResult
from project_buffer.domain.enums import (
    DeliveryStatus,
    Direction,
    ProcessingStatus,
    SenderStatus,
    Urgency,
)
from project_buffer.infrastructure.db.models import (
    DeliveryEvent,
    Draft,
    Message,
    MessageAnalysis,
    User,
)
from project_buffer.services import auth
from project_buffer.services.container import Services
from project_buffer.services.conversations import ensure_conversation

DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo-password-123"  # noqa: S105 - development-only fixture
DEMO_TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # noqa: S105 - published example secret, dev only


def demo_messages(sender: str) -> list[tuple[str, int, MessageAnalysisResult]]:
    """(original text, minutes ago, analysis) for each demo inbound message."""
    return [
        (
            "Can you pick Riley up from soccer practice on Thursday at 5:30? I have a work "
            "meeting that will run late.",
            60 * 26,
            MessageAnalysisResult.minimal(
                short_summary=f"{sender} asks whether you can pick Riley up from soccer "
                "practice on Thursday at 5:30 PM.",
                topic="Soccer practice pickup",
                children_involved=["Riley"],
                requests=["Pick Riley up from soccer practice on Thursday at 5:30 PM."],
                dates_and_times=[{"what": "Soccer practice pickup", "when": "Thursday at 5:30 PM"}],
                sender_statements=[f"{sender} reports having a work meeting that will run late."],
                response_needed=True,
                schedule_issue=True,
            ),
        ),
        (
            "You are unbelievable. You never lift a finger for these kids and everyone knows "
            "what a pathetic excuse for a parent you are. My dad can't drive Wednesday and I "
            "STILL don't have a car thanks to you, so you need to take them to school and "
            "pick them up. And don't even think about putting them on that bus. Not that "
            "you care about anyone but yourself and your stupid girlfriend.",
            60 * 20,
            MessageAnalysisResult.minimal(
                short_summary=f"{sender} asks you to drive the children to and from school "
                "on Wednesday.",
                topic="School transportation",
                requests=["Transportation to and from school on Wednesday."],
                dates_and_times=[{"what": "School transportation", "when": "Wednesday"}],
                sender_statements=[
                    f"{sender} reports that her father will be unavailable to drive on Wednesday.",
                    f"{sender} reports that she still does not have a vehicle.",
                ],
                sender_positions=[f"{sender} does not want the children to use the school bus."],
                response_needed=True,
                response_deadline="Before Wednesday",
                school_issue=True,
                schedule_issue=True,
                omitted_content_present=True,
                omitted_content_categories=[
                    "insults",
                    "parenting_attacks",
                    "attacks_on_partner",
                    "guilt_or_shaming",
                ],
            ),
        ),
        (
            "Sam has a dentist appointment Tuesday at 3:30 at Lakeside Dental. Try to "
            "actually remember this one for once.",
            60 * 9,
            MessageAnalysisResult.minimal(
                short_summary="Sam has a dentist appointment on Tuesday at 3:30 PM at "
                "Lakeside Dental.",
                topic="Sam: dentist appointment",
                children_involved=["Sam"],
                factual_information=[
                    "Sam has a dentist appointment on Tuesday at 3:30 PM at Lakeside Dental."
                ],
                dates_and_times=[
                    {"what": "Sam's dentist appointment", "when": "Tuesday at 3:30 PM"}
                ],
                locations=["Lakeside Dental"],
                urgency=Urgency.INFORMATIONAL,
                medical_issue=True,
                omitted_content_present=True,
                omitted_content_categories=["rhetorical_attacks"],
            ),
        ),
        (
            "Sam fell off the climbing frame at school and they think his arm is broken. The "
            "school called an ambulance and they are taking him to Mercy General ER. Get "
            "there NOW. This is what happens when nobody teaches him to be careful.",
            35,
            MessageAnalysisResult.minimal(
                short_summary="Sam fell at school and may have a broken arm. An ambulance is "
                "taking him to the emergency room at Mercy General.",
                topic="Sam injured at school",
                children_involved=["Sam"],
                requests=["Go to Mercy General emergency room now."],
                factual_information=[
                    "Sam fell off the climbing frame at school.",
                    "The school called an ambulance.",
                    "Sam is being taken to the emergency room at Mercy General.",
                ],
                sender_statements=["The school thinks Sam's arm is broken."],
                locations=["Mercy General emergency room"],
                response_needed=True,
                urgency=Urgency.EMERGENCY,
                safety_issue=True,
                medical_issue=True,
                school_issue=True,
                omitted_content_present=True,
                omitted_content_categories=["parenting_attacks"],
            ),
        ),
        (
            "You violated the custody order by keeping them until Monday. They were supposed "
            "to be back Sunday at 6 and you know it. I am writing all of this down for my "
            "lawyer. You think the rules don't apply to you.",
            60 * 3,
            MessageAnalysisResult.minimal(
                short_summary=f"{sender} alleges that the custody schedule was not followed "
                "last weekend.",
                topic="Custody schedule allegation",
                allegations_relevant_to_parenting_or_legal_matters=[
                    f"{sender} alleges that the custody order was violated by the children "
                    "remaining with you until Monday.",
                    f"{sender} alleges that the children were due back on Sunday at 6 PM.",
                ],
                commitments_made_by_sender=[
                    f"{sender} says she is documenting this for her lawyer."
                ],
                dates_and_times=[
                    {"what": "Return time alleged by sender", "when": "Sunday at 6 PM"}
                ],
                legal_or_custody_issue=True,
                schedule_issue=True,
                omitted_content_present=True,
                omitted_content_categories=["character_attacks"],
            ),
        ),
    ]


def seed_demo(session: Session, services: Services, now: datetime) -> int:
    """Insert the demo conversation and a demo owner. Returns the number of messages added."""
    settings = services.settings
    if settings.is_production:
        raise RuntimeError("demo data cannot be loaded in production")
    if session.scalar(select(func.count()).select_from(Message)):
        return 0

    if session.scalar(select(func.count()).select_from(User)) == 0:
        user = auth.create_owner(session, DEMO_USERNAME, DEMO_PASSWORD)
        user.totp_secret_encrypted = services.crypto.encrypt(
            DEMO_TOTP_SECRET.encode(), aad=f"totp:{user.id}".encode()
        )
        user.totp_enabled_at = now

    conversation = ensure_conversation(session, settings)
    sender = settings.coparent_display_name
    inbound: list[Message] = []
    for index, (text, minutes_ago, result) in enumerate(demo_messages(sender)):
        message_id = uuid.uuid4()
        received = now - timedelta(minutes=minutes_ago)
        message = Message(
            id=message_id,
            conversation_id=conversation.id,
            direction=Direction.INBOUND,
            provider="twilio",
            provider_message_id=f"SMdemo{index:028d}",
            from_number=settings.coparent_phone_number,
            to_number=settings.twilio_phone_number,
            body_ciphertext=b"",
            body_mac=services.crypto.mac(text.encode()),
            provider_metadata={"demo": True},
            occurred_at=received,
            sender_status=SenderStatus.AUTHORIZED,
            processing_status=ProcessingStatus.PROCESSED,
            processed_at=received + timedelta(seconds=8),
            urgency=result.urgency,
            requires_response=result.response_needed,
        )
        message.body_ciphertext = services.crypto.encrypt(text.encode(), aad=message.body_aad())
        session.add(message)
        session.add(
            MessageAnalysis(
                message_id=message_id,
                llm_provider="demo",
                llm_model="demo",
                prompt_version="demo",
                short_summary=result.short_summary,
                topic=result.topic,
                structured=result.model_dump(mode="json"),
                search_text="\n".join(result.text_fields()),
                safety_keywords_detected=result.urgency == Urgency.EMERGENCY,
            )
        )
        inbound.append(message)
    session.flush()

    # A sent reply to the first message, with delivery callbacks.
    first = inbound[0]
    reply_text = "Yes, I can pick Riley up from soccer on Thursday at 5:30."
    sent_at = first.occurred_at + timedelta(minutes=40)
    reply = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        direction=Direction.OUTBOUND,
        provider="twilio",
        provider_message_id="SMdemo" + "9" * 28,
        from_number=settings.twilio_phone_number,
        to_number=settings.coparent_phone_number,
        body_ciphertext=b"",
        body_mac=services.crypto.mac(reply_text.encode()),
        occurred_at=sent_at,
        sender_status=SenderStatus.OWNER,
        processing_status=ProcessingStatus.NOT_APPLICABLE,
        delivery_status=DeliveryStatus.DELIVERED,
        sent_at=sent_at,
        in_reply_to_id=first.id,
        read_at=sent_at,
        handled_at=sent_at,
    )
    reply.body_ciphertext = services.crypto.encrypt(reply_text.encode(), aad=reply.body_aad())
    session.add(reply)
    for offset, status in ((1, "sent"), (4, "delivered")):
        session.add(
            DeliveryEvent(
                message_id=reply.id,
                provider_message_id=reply.provider_message_id or "",
                status=status,
                received_at=sent_at + timedelta(seconds=offset),
            )
        )
    first.read_at = first.handled_at = sent_at
    first.requires_response = False
    session.add(
        Draft(
            conversation_id=conversation.id,
            in_reply_to_id=first.id,
            instruction="Yes I can get Riley Thursday.",
            ai_draft_text=reply_text,
            body=reply_text,
            status="sent",
            approved_at=sent_at,
            sent_message_id=reply.id,
        )
    )

    # A draft reply to the transportation request, waiting for approval.
    draft_text = (
        "We aren't available to provide transportation on Wednesday. I continue to support "
        "using the school bus as a reliable option if you'd like to revisit that."
    )
    session.add(
        Draft(
            conversation_id=conversation.id,
            in_reply_to_id=inbound[1].id,
            instruction="We can't do Wednesday. Remind her I'm still okay with the bus.",
            ai_draft_text=draft_text,
            ai_notes=[],
            body=draft_text,
        )
    )
    return len(inbound) + 1


def demo_totp_code() -> str:
    return pyotp.TOTP(DEMO_TOTP_SECRET).now()
