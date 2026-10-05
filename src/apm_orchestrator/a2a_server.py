"""Expose the APM Supervisor as an A2A (Agent2Agent protocol) agent.

MCP and A2A solve different problems, and APM now speaks both:
- MCP is agent-to-*tool*: `tools.py` registers every `apm_connectors`
  route as an MCP tool an agent can call.
- A2A is agent-to-*agent*: another organisation's agent (a CRM copilot,
  a ticketing assistant, another team's orchestrator) discovers this
  Supervisor through its Agent Card at `/.well-known/agent-card.json`
  and hands it a business request over JSON-RPC (`SendMessage`), the
  same way a person types one into `cli.py`.

What an A2A caller gets is exactly what a human caller of
`run_supervisor` gets, and nothing more: business-intent routing to the
Order-Renewal or Customer-Onboarding agent, whose writes only ever
*propose* pending actions in `apm_connectors`. Nothing here exposes
`ConnectorsClient.decide_action` or any other way to approve a pending
action -- the approval boundary in CLAUDE.md doesn't move just because
the caller is another agent instead of a person. The task's final text
tells the caller what was proposed; a human still approves it.

Calling the Supervisor costs real model spend, so the JSON-RPC route
requires a bearer token whenever `A2A_API_KEY` is set (the Agent Card
stays public, as discovery requires). Leave it unset only for local
development.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from a2a.helpers import get_message_text, new_task, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
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
    TaskState,
)
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

RPC_PATH = "/a2a"

SupervisorFn = Callable[[str], Awaitable[str]]


def build_agent_card(base_url: str, *, require_auth: bool) -> AgentCard:
    """The Agent Card another agent reads to discover what this one does.

    Skills mirror `supervisor.py`'s delegate tools one to one -- a new
    business-process agent there should get a matching skill here.
    """
    card = AgentCard(
        name="APM Supervisor",
        description=(
            "Routes a business request to the APM agent that owns that "
            "business process end to end. Writes to Gmail, Calendar, Drive, "
            "Salesforce or Jira are only ever proposed: each one waits for "
            "a human to approve it in apm_connectors before it executes."
        ),
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(url=base_url.rstrip("/") + RPC_PATH, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="order_renewal",
                name="Order renewal",
                description=(
                    "Renew, extend or re-up an existing customer's contract, "
                    "licence or subscription: verifies the account and "
                    "opportunity in Salesforce, checks Jira for blocking "
                    "tickets, and proposes the renewal actions for human approval."
                ),
                tags=["renewal", "salesforce", "jira", "human-in-the-loop"],
                examples=["Acme Ltd wants to renew their annual licence before it lapses next month."],
            ),
            AgentSkill(
                id="customer_onboarding",
                name="Customer onboarding",
                description=(
                    "Kick off onboarding for a newly closed-won new-business "
                    "customer, proposing the kickoff actions for human approval."
                ),
                tags=["onboarding", "salesforce", "calendar", "human-in-the-loop"],
                examples=["We just closed Globex as a new customer; start their onboarding."],
            ),
        ],
    )
    if require_auth:
        card.security_schemes["bearer"].CopyFrom(
            SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer"))
        )
        card.security_requirements.append(SecurityRequirement(schemes={"bearer": {}}))
    return card


class SupervisorAgentExecutor(AgentExecutor):
    """Runs one Supervisor turn per A2A task.

    `supervisor` is injectable so tests (and anything else that wants a
    fake) never need the Claude API; it defaults to the real
    `run_supervisor`.
    """

    def __init__(self, supervisor: SupervisorFn | None = None) -> None:
        self._supervisor = supervisor

    async def _run(self, prompt: str) -> str:
        if self._supervisor is not None:
            return await self._supervisor(prompt)
        # Imported lazily: the real Supervisor pulls in the Claude Agent
        # SDK, which tests that inject a fake shouldn't need to load.
        from apm_orchestrator.supervisor import run_supervisor

        return await run_supervisor(prompt)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED, history=[context.message])
            )
        updater = TaskUpdater(event_queue, task_id, context_id)

        prompt = get_message_text(context.message).strip() if context.message else ""
        if not prompt:
            await updater.reject(
                new_text_message("Send the business request as a text part.", context_id=context_id, task_id=task_id)
            )
            return

        await updater.start_work()
        result = await self._run(prompt)
        await updater.add_artifact([new_text_part(result, media_type="text/plain")], name="supervisor_result")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # A Supervisor turn is one short model call; there's nothing to
        # interrupt cleanly mid-turn, so cancellation just marks it.
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_key: str) -> None:
        super().__init__(app)
        self._expected = f"Bearer {api_key}".encode()

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(RPC_PATH):
            supplied = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(supplied, self._expected):
                return JSONResponse({"error": "missing or invalid bearer token"}, status_code=401)
        return await call_next(request)


def build_app(
    *,
    base_url: str = "http://localhost:9999",
    supervisor: SupervisorFn | None = None,
    api_key: str | None = None,
) -> Starlette:
    """The A2A Starlette app: Agent Card plus the JSON-RPC endpoint."""
    card = build_agent_card(base_url, require_auth=bool(api_key))
    handler = DefaultRequestHandlerV2(
        agent_executor=SupervisorAgentExecutor(supervisor),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, RPC_PATH)])
    if api_key:
        app.add_middleware(_BearerAuthMiddleware, api_key=api_key)
    return app


def main() -> None:
    import uvicorn

    from apm_orchestrator.config import load_settings

    load_settings()  # same .env loading every other entry point does
    host = os.environ.get("A2A_HOST", "127.0.0.1")
    port = int(os.environ.get("A2A_PORT", "9999"))
    base_url = os.environ.get("A2A_PUBLIC_URL", f"http://{host}:{port}")
    uvicorn.run(build_app(base_url=base_url, api_key=os.environ.get("A2A_API_KEY") or None), host=host, port=port)


if __name__ == "__main__":
    main()
