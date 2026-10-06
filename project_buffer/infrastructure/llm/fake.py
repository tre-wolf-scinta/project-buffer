"""Development-only provider. Refused by production configuration.

It never looks at the message text, so it cannot leak it.
"""

from __future__ import annotations

from project_buffer.domain.analysis import DraftReplyResult, MessageAnalysisResult
from project_buffer.domain.enums import Confidence
from project_buffer.infrastructure.llm.base import AnalysisRequest, DraftRequest


class FakeProvider:
    name = "fake"
    model = "fake"

    def analyze_message(self, request: AnalysisRequest) -> MessageAnalysisResult:
        return MessageAnalysisResult.minimal(
            short_summary=(
                f"A message from {request.sender_name} was received. AI filtering is not "
                "configured in this environment, so it has not been summarized."
            ),
            topic="Unsummarized message",
            confidence=Confidence.LOW,
            ambiguity_notes=["Development mode: no AI provider is configured."],
        )

    def draft_reply(self, request: DraftRequest) -> DraftReplyResult:
        return DraftReplyResult(
            message_text=request.instruction.strip()[:1200] or "(empty)",
            notes_for_owner=["Development mode: your instruction was copied without AI drafting."],
        )
