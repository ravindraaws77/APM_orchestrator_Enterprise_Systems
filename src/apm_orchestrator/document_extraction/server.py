"""The document-extraction agent as an A2A server.

Publishes an Agent Card at `/.well-known/agent-card.json` and serves the
A2A JSON-RPC endpoint at `/a2a`. One skill today,
`extract_purchase_order`.

Task lifecycle:

1. A caller sends a message with the document as a raw part
   (bytes + media type + filename) and a data part carrying the skill id
   and the caller's review policy (`min_field_confidence`,
   `required_fields`). The policy comes from the caller's own
   `policy.yaml`; this agent has no business policy of its own.
2. The agent extracts, attaches the result as the
   `purchase_order_extraction` artifact, and either completes the task
   or -- if any field needs a human -- moves it to `input-required`,
   with the reasons in the status message.
3. For an `input-required` task, a follow-up message on the same task
   carries a reviewer's decision: `{"decision": "accept",
   "reviewer": ..., "reviewed_fields": {...}}` updates the artifact and
   completes the task; `{"decision": "reject", ...}` rejects it.

Run it:
    apm-document-extraction-agent --port 8100
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
from typing import Any

import anthropic
from a2a.helpers.proto_helpers import (
    get_data_parts,
    new_data_part,
    new_task_from_user_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    TaskState,
)
from starlette.applications import Starlette

from apm_orchestrator.config import Settings, load_settings
from apm_orchestrator.document_extraction.extractor import (
    SUPPORTED_MEDIA_TYPES,
    ClaudeExtractor,
    ExtractionError,
    Extractor,
)
from apm_orchestrator.document_extraction.schema import (
    ARTIFACT_NAME,
    PO_FIELDS,
    SKILL_ID,
    ExtractionSchemaError,
    apply_review,
    parse_extraction,
    review_reasons,
)

logger = logging.getLogger("apm_orchestrator.document_extraction")

RPC_PATH = "/a2a"
DEFAULT_MIN_FIELD_CONFIDENCE = 0.8
DEFAULT_REQUIRED_FIELDS = ["account_name", "po_number", "amount"]


def build_agent_card(public_url: str, require_auth: bool) -> AgentCard:
    card = AgentCard(
        name="APM Document Extraction Agent",
        description=(
            "Extracts structured, confidence-scored fields from business documents. Returns "
            "results as A2A task artifacts; low-confidence results move the task to "
            "input-required for human review. Holds no credentials to any business system "
            "and never writes anywhere."
        ),
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(url=public_url.rstrip("/") + RPC_PATH, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=list(SUPPORTED_MEDIA_TYPES),
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=SKILL_ID,
                name="Extract purchase order",
                description=(
                    "Purchase order or signed renewal quote -> "
                    + ", ".join(PO_FIELDS)
                    + ", each with a 0-1 confidence and a verbatim evidence snippet. Send the "
                    "document as a raw part and a data part {skill, min_field_confidence, "
                    "required_fields}."
                ),
                tags=["idp", "purchase-order", "extraction"],
                input_modes=list(SUPPORTED_MEDIA_TYPES),
                output_modes=["application/json"],
            )
        ],
    )
    if require_auth:
        card.security_schemes["bearer"].CopyFrom(
            SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer"))
        )
        card.security_requirements.append(SecurityRequirement(schemes={"bearer": StringList()}))
    return card


def _artifact_data(task: Any) -> dict[str, Any] | None:
    for artifact in reversed(list(task.artifacts)):
        if artifact.name == ARTIFACT_NAME:
            parts = get_data_parts(artifact.parts)
            if parts:
                return parts[0]
    return None


class DocumentExtractionExecutor(AgentExecutor):
    def __init__(self, extractor: Extractor, max_document_bytes: int) -> None:
        self._extractor = extractor
        self._max_document_bytes = max_document_bytes

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
            await self._extract(context, TaskUpdater(event_queue, task.id, task.context_id))
            return

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        if task.status.state != TaskState.TASK_STATE_INPUT_REQUIRED:
            await updater.reject(updater.new_agent_message([new_text_part("task is not awaiting review")]))
            return
        await self._apply_review(context, task, updater)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is not None:
            await TaskUpdater(event_queue, task.id, task.context_id).cancel()

    async def _extract(self, context: RequestContext, updater: TaskUpdater) -> None:
        message = context.message
        params = next(iter(get_data_parts(message.parts)), {}) or {}
        document_part = next((p for p in message.parts if p.raw), None)

        async def reject(reason: str) -> None:
            await updater.reject(updater.new_agent_message([new_text_part(reason)]))

        if params.get("skill", SKILL_ID) != SKILL_ID:
            return await reject(f"unknown skill {params.get('skill')!r}; this agent offers {SKILL_ID!r}")
        if document_part is None:
            return await reject("no document: send it as a raw part (bytes + media_type)")
        if document_part.media_type not in SUPPORTED_MEDIA_TYPES:
            return await reject(
                f"unsupported media type {document_part.media_type!r}; supported: {list(SUPPORTED_MEDIA_TYPES)}"
            )
        if len(document_part.raw) > self._max_document_bytes:
            return await reject(f"document is {len(document_part.raw)} bytes; limit is {self._max_document_bytes}")

        try:
            min_confidence = float(params.get("min_field_confidence", DEFAULT_MIN_FIELD_CONFIDENCE))
        except (TypeError, ValueError):
            return await reject("min_field_confidence must be a number")
        required = list(params.get("required_fields") or DEFAULT_REQUIRED_FIELDS)
        unknown = set(required) - set(PO_FIELDS)
        if unknown:
            return await reject(f"unknown required_fields: {sorted(unknown)}")

        await updater.start_work()
        try:
            extraction = await self._extractor.extract_purchase_order(
                document_part.raw, document_part.media_type, document_part.filename or None
            )
        except (ExtractionError, ExtractionSchemaError, anthropic.APIError) as exc:
            logger.warning("extraction failed for task %s: %s", updater.task_id, exc)
            await updater.failed(updater.new_agent_message([new_text_part(f"extraction failed: {exc}")]))
            return

        reasons = review_reasons(extraction, min_confidence, required)
        await updater.add_artifact(
            [
                new_data_part(
                    {
                        "extraction": extraction.to_dict(),
                        "review": {
                            "reasons": reasons,
                            "min_field_confidence": min_confidence,
                            "required_fields": required,
                        },
                    }
                )
            ],
            artifact_id=ARTIFACT_NAME,
            name=ARTIFACT_NAME,
        )
        if reasons:
            await updater.requires_input(
                updater.new_agent_message(
                    [
                        new_text_part("Human review required before this extraction is used."),
                        new_data_part({"review_reasons": reasons}),
                    ]
                )
            )
        else:
            await updater.complete()

    async def _apply_review(self, context: RequestContext, task: Any, updater: TaskUpdater) -> None:
        review = next(iter(get_data_parts(context.message.parts)), {}) or {}
        previous = _artifact_data(task)
        if previous is None:
            await updater.failed(updater.new_agent_message([new_text_part("no extraction to review")]))
            return

        reviewer = review.get("reviewer")
        if review.get("decision") == "reject":
            reason = review.get("reason") or "rejected by reviewer"
            await updater.reject(updater.new_agent_message([new_text_part(f"{reason} (reviewer: {reviewer})")]))
            return

        reasons = previous.get("review", {}).get("reasons", {})
        try:
            if review.get("decision") != "accept":
                raise ValueError('decision must be "accept" or "reject"')
            reviewed = apply_review(
                parse_extraction(previous["extraction"]),
                reviewed_fields=dict(review.get("reviewed_fields") or {}),
                reviewer=reviewer or "",
                flagged_fields=[name for name in reasons if not name.startswith("_")],
            )
        except ValueError as exc:
            # Stays input-required: a bad review is the reviewer's to fix,
            # not a reason to lose the extraction.
            await updater.requires_input(
                updater.new_agent_message([new_text_part(f"review not applied: {exc}")])
            )
            return

        await updater.add_artifact(
            [
                new_data_part(
                    {
                        "extraction": reviewed.to_dict(),
                        "review": {**previous.get("review", {}), "resolved_by": reviewer},
                    }
                )
            ],
            artifact_id=ARTIFACT_NAME,
            name=ARTIFACT_NAME,
        )
        await updater.complete()


class _BearerAuthMiddleware:
    """Requires `Authorization: Bearer <key>` on the JSON-RPC endpoint when
    keys are configured. The Agent Card stays public, as A2A expects --
    it's how a caller learns that auth is required."""

    def __init__(self, app: Any, keys: tuple[str, ...]) -> None:
        self._app = app
        self._keys = keys

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] == RPC_PATH and self._keys:
            headers = dict(scope.get("headers") or [])
            supplied = headers.get(b"authorization", b"").decode("latin-1")
            token = supplied[7:] if supplied.lower().startswith("bearer ") else ""
            if not any(hmac.compare_digest(token, key) for key in self._keys):
                body = json.dumps({"error": "missing or invalid bearer token"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self._app(scope, receive, send)


def build_app(
    extractor: Extractor | None = None,
    settings: Settings | None = None,
    public_url: str | None = None,
) -> Any:
    settings = settings or load_settings()
    extractor = extractor or ClaudeExtractor(settings)
    card = build_agent_card(
        public_url or settings.document_extraction_url,
        require_auth=bool(settings.document_extraction_server_api_keys),
    )
    handler = DefaultRequestHandlerV2(
        agent_executor=DocumentExtractionExecutor(extractor, settings.document_extraction_max_bytes),
        # In-memory: tasks (including the submitted document) live only as
        # long as this process. Fine for a pilot; a durable store with a
        # retention policy is a prerequisite for real customer documents
        # (roadmap Phase 4's PII/retention item).
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, RPC_PATH))
    return _BearerAuthMiddleware(app, settings.document_extraction_server_api_keys)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument(
        "--public-url",
        default=None,
        help=(
            "URL callers reach this agent at, advertised in the Agent Card -- A2A clients send "
            "requests to the card's URL, so it must be reachable "
            "(default: DOCUMENT_EXTRACTION_URL if set, else http://HOST:PORT)"
        ),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = load_settings()
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is required to run the document-extraction agent")
    public_url = args.public_url or os.environ.get("DOCUMENT_EXTRACTION_URL") or f"http://{args.host}:{args.port}"
    uvicorn.run(build_app(settings=settings, public_url=public_url), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
