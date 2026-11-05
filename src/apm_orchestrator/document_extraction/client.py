"""The A2A client a business agent uses to call the document-extraction
agent.

Everything that comes back is treated as untrusted: the artifact is
re-validated with `schema.parse_extraction`, the task state is mapped to
a small closed set, and anything unexpected becomes `state="failed"`
rather than an exception a case graph has to know about.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.errors import A2AClientError
from a2a.helpers.proto_helpers import get_data_parts, get_text_parts, new_data_part
from a2a.types import Message, Part, Role, SendMessageRequest, TaskState

from apm_orchestrator.config import Settings, load_settings
from apm_orchestrator.document_extraction.schema import (
    ARTIFACT_NAME,
    SKILL_ID,
    ExtractionSchemaError,
    PurchaseOrderExtraction,
    parse_extraction,
)

_STATES = {
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    TaskState.TASK_STATE_REJECTED: "rejected",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_CANCELED: "failed",
}


@dataclass(frozen=True)
class ExtractionTaskResult:
    """One A2A task's outcome, reduced to what a case graph needs."""

    task_id: str
    context_id: str
    state: str  # "completed" | "input_required" | "rejected" | "failed"
    extraction: PurchaseOrderExtraction | None
    review_reasons: dict[str, str] = field(default_factory=dict)
    message: str = ""

    def to_state(self) -> dict[str, Any]:
        """A JSON-safe dict for a LangGraph checkpoint."""
        return {
            "task_id": self.task_id,
            "context_id": self.context_id,
            "state": self.state,
            "extraction": self.extraction.to_dict() if self.extraction else None,
            "review_reasons": dict(self.review_reasons),
            "message": self.message,
        }


class DocumentExtractionClient:
    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        headers = {}
        if self._settings.document_extraction_api_key:
            headers["Authorization"] = f"Bearer {self._settings.document_extraction_api_key}"
        self._http = httpx.AsyncClient(
            base_url=self._settings.document_extraction_url,
            headers=headers,
            timeout=httpx.Timeout(300.0, connect=10.0),
            transport=transport,
        )
        self._a2a = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _client(self):
        if self._a2a is None:
            card = await A2ACardResolver(self._http, self._settings.document_extraction_url).get_agent_card()
            if not any(skill.id == SKILL_ID for skill in card.skills):
                raise RuntimeError(f"agent at {self._settings.document_extraction_url} doesn't offer {SKILL_ID!r}")
            self._a2a = ClientFactory(ClientConfig(httpx_client=self._http, streaming=False)).create(card)
        return self._a2a

    async def _send(self, message: Message) -> ExtractionTaskResult:
        task = None
        try:
            client = await self._client()
            async for event in client.send_message(SendMessageRequest(message=message)):
                if event.HasField("task"):
                    task = event.task
        except (A2AClientError, httpx.HTTPError, RuntimeError) as exc:
            # Unreachable agent, auth failure, protocol error: a clean
            # "failed" outcome the case graph can stop on, not a crash.
            return ExtractionTaskResult(
                message.task_id, message.context_id, "failed", None,
                message=f"document-extraction agent call failed: {exc}",
            )
        if task is None:
            return ExtractionTaskResult("", "", "failed", None, message="agent returned no task")
        return _to_result(task)

    async def extract_purchase_order(
        self,
        document: bytes,
        media_type: str,
        filename: str | None,
        min_field_confidence: float,
        required_fields: list[str],
    ) -> ExtractionTaskResult:
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[
                Part(raw=document, media_type=media_type, filename=filename or ""),
                new_data_part(
                    {
                        "skill": SKILL_ID,
                        "min_field_confidence": min_field_confidence,
                        "required_fields": list(required_fields),
                    }
                ),
            ],
        )
        return await self._send(message)

    async def submit_review(
        self,
        task_id: str,
        context_id: str,
        reviewer: str,
        decision: str,
        reviewed_fields: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> ExtractionTaskResult:
        """Answer an input-required task with a reviewer's decision
        ("accept", optionally with corrected field values, or "reject")."""
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            task_id=task_id,
            context_id=context_id,
            parts=[
                new_data_part(
                    {
                        "decision": decision,
                        "reviewer": reviewer,
                        "reviewed_fields": dict(reviewed_fields or {}),
                        "reason": reason or "",
                    }
                )
            ],
        )
        return await self._send(message)


def _to_result(task: Any) -> ExtractionTaskResult:
    state = _STATES.get(task.status.state)
    status_text = " ".join(get_text_parts(task.status.message.parts)) if task.status.HasField("message") else ""
    if state is None:
        return ExtractionTaskResult(
            task.id, task.context_id, "failed", None,
            message=f"unexpected task state {TaskState.Name(task.status.state)}",
        )

    extraction = None
    reasons: dict[str, str] = {}
    artifact = next((a for a in reversed(list(task.artifacts)) if a.name == ARTIFACT_NAME), None)
    if artifact is not None:
        data = next(iter(get_data_parts(artifact.parts)), None)
        try:
            if not isinstance(data, dict):
                raise ExtractionSchemaError("artifact has no data part")
            extraction = parse_extraction(data.get("extraction"))
            raw_reasons = (data.get("review") or {}).get("reasons") or {}
            reasons = {str(k): str(v) for k, v in raw_reasons.items()} if isinstance(raw_reasons, dict) else {}
        except ExtractionSchemaError as exc:
            return ExtractionTaskResult(task.id, task.context_id, "failed", None, message=f"invalid artifact: {exc}")

    if state in ("completed", "input_required") and extraction is None:
        return ExtractionTaskResult(task.id, task.context_id, "failed", None, message="no extraction artifact")
    if state == "completed":
        reasons = {}  # resolved: either none were raised or a reviewer answered them
    return ExtractionTaskResult(task.id, task.context_id, state, extraction, reasons, status_text)


_client: DocumentExtractionClient | None = None


def get_extraction_client() -> DocumentExtractionClient:
    """Module-level singleton, mirroring tools.get_client() -- and, like it,
    the seam tests monkeypatch."""
    global _client
    if _client is None:
        _client = DocumentExtractionClient()
    return _client


async def aclose_extraction_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
