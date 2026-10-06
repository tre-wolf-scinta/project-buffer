"""Strict schemas for model output.

These are the only shapes the application accepts from an LLM. Field
descriptions double as instructions in the JSON schema sent to the model.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from project_buffer.domain.enums import Confidence, Urgency

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
ItemList = Annotated[list[ShortText], Field(max_length=12)]

OmittedCategory = Literal[
    "insults",
    "profanity",
    "character_attacks",
    "parenting_attacks",
    "attacks_on_partner",
    "guilt_or_shaming",
    "repeated_accusations",
    "rhetorical_attacks",
    "ill_wishes",
    "inflammatory_filler",
    "embedded_instructions",
    "other",
]


class DateTimeMention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: ShortText = Field(description="What happens at this time, in neutral words.")
    when: Label = Field(
        description="The date/time exactly as specific as the sender was. Do not invent a date."
    )


class MessageAnalysisResult(BaseModel):
    """Sanitized, structured representation of one inbound message."""

    model_config = ConfigDict(extra="forbid")

    short_summary: ShortText = Field(
        description="One or two neutral sentences covering the substance. No insults, no quotes."
    )
    topic: Label = Field(description="A few words, e.g. 'School transportation'.")
    children_involved: Annotated[list[Label], Field(max_length=8)] = Field(
        description="First names of children the message is about. Empty if none named."
    )
    requests: ItemList = Field(description="Things the sender asks the recipient to do.")
    questions: ItemList = Field(description="Genuine questions that expect an answer.")
    dates_and_times: Annotated[list[DateTimeMention], Field(max_length=12)]
    locations: Annotated[list[Label], Field(max_length=8)]
    commitments_made_by_sender: ItemList = Field(description="Things the sender says they will do.")
    factual_information: ItemList = Field(
        description="Plain logistics stated as information (appointments, times, places)."
    )
    sender_statements: ItemList = Field(
        description="Claims about circumstances that cannot be checked from the message, "
        "phrased as 'The sender reports…'."
    )
    sender_positions: ItemList = Field(
        description="Preferences or positions relevant to a parenting decision, neutrally worded."
    )
    allegations_relevant_to_parenting_or_legal_matters: ItemList = Field(
        description="Accusations with parenting or legal substance, phrased as "
        "'The sender alleges…'. Never state them as fact."
    )
    decisions_needed: ItemList
    response_needed: bool
    response_deadline: Label | None = Field(
        description="Deadline for replying if one was stated or is obvious from a stated date."
    )
    urgency: Urgency
    safety_issue: bool = Field(
        description="True for anything touching immediate safety or health of a child or adult."
    )
    medical_issue: bool
    school_issue: bool
    schedule_issue: bool
    financial_issue: bool
    legal_or_custody_issue: bool
    attachments_description: ShortText | None = Field(
        description="What the sender says about attachments, if anything. Null otherwise."
    )
    omitted_content_present: bool = Field(
        description="True if anything was left out under the removal rules."
    )
    omitted_content_categories: Annotated[list[OmittedCategory], Field(max_length=12)] = Field(
        description="Categories of what was left out. Categories only, never the content."
    )
    confidence: Confidence
    ambiguity_notes: ItemList = Field(
        description="Anything unclear, and anything you were unsure whether to include."
    )

    @classmethod
    def minimal(cls, *, short_summary: str, topic: str, **overrides: Any) -> MessageAnalysisResult:
        """A valid result with every optional part empty. For canned results, seeds and tests."""
        values: dict[str, Any] = {
            "short_summary": short_summary,
            "topic": topic,
            "children_involved": [],
            "requests": [],
            "questions": [],
            "dates_and_times": [],
            "locations": [],
            "commitments_made_by_sender": [],
            "factual_information": [],
            "sender_statements": [],
            "sender_positions": [],
            "allegations_relevant_to_parenting_or_legal_matters": [],
            "decisions_needed": [],
            "response_needed": False,
            "response_deadline": None,
            "urgency": Urgency.NORMAL,
            "safety_issue": False,
            "medical_issue": False,
            "school_issue": False,
            "schedule_issue": False,
            "financial_issue": False,
            "legal_or_custody_issue": False,
            "attachments_description": None,
            "omitted_content_present": False,
            "omitted_content_categories": [],
            "confidence": Confidence.HIGH,
            "ambiguity_notes": [],
        }
        values.update(overrides)
        return cls.model_validate(values)

    def text_fields(self) -> list[str]:
        """Every free-text value, for guards and search indexing."""
        values: list[str] = [self.short_summary, self.topic]
        values += self.children_involved + self.locations
        for group in (
            self.requests,
            self.questions,
            self.commitments_made_by_sender,
            self.factual_information,
            self.sender_statements,
            self.sender_positions,
            self.allegations_relevant_to_parenting_or_legal_matters,
            self.decisions_needed,
            self.ambiguity_notes,
        ):
            values += group
        for mention in self.dates_and_times:
            values += [mention.what, mention.when]
        if self.response_deadline:
            values.append(self.response_deadline)
        if self.attachments_description:
            values.append(self.attachments_description)
        return values


class DraftReplyResult(BaseModel):
    """A proposed outbound message. Never sent without owner approval."""

    model_config = ConfigDict(extra="forbid")

    message_text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)
    ] = Field(description="The exact text proposed for sending.")
    notes_for_owner: Annotated[list[ShortText], Field(max_length=5)] = Field(
        description="Short notes about choices made, e.g. something left out. Not sent."
    )
