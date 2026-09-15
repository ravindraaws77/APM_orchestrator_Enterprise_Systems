# APM Orchestrator

The avatar + multi-agent reasoning layer for APM (Agentic Process
Management). Holds **all** reasoning and orchestration; the connector
layer it calls into
([`apm_connectors`](https://github.com/ravindraaws77/apm_connectors_enterprise_systems))
stays reasoning-free by design — see that repo's `CLAUDE.md` and
`docs/roadmap.md` for the two-repo split this project is the other half
of.

## What's here (Phase 1)

- **Supervisor** (`src/apm_orchestrator/supervisor.py`) — does
  business-intent routing only (e.g. "this is a renewal"), never calls a
  connector tool itself. Delegates to specialized agents.
- **Order-Renewal agent** (`src/apm_orchestrator/agents/order_renewal/`)
  — the pilot business-process agent: detect → verify → act → record,
  using a scoped toolbelt across Gmail/Calendar/Drive/Salesforce/Jira.
  Its policy (what counts as "a renewal", SLA windows, blocking-ticket
  rules, follow-up routing) lives in `policy.yaml`, not code.
- **Connectors client** (`src/apm_orchestrator/connectors_client.py`) —
  the only thing in this repo that talks to `apm_connectors`, over plain
  HTTP against its `/tools/*` API. Every write call only *proposes* a
  pending action; nothing executes until a human approves it directly
  against `apm_connectors`' own decision endpoint.
- **Tools** (`src/apm_orchestrator/tools.py`) — one Claude Agent SDK
  `@tool` per `apm_connectors` route, registered on one MCP server. Each
  agent is handed only the subset of these tools its business process
  needs (its toolbelt) via `allowed_tools` — least-privilege scoping
  that maps to something real ("the Renewals agent can touch Opportunity
  records and send renewal mail," not "this agent can call the
  Salesforce API").

**The approval boundary never moves.** No tool here ever calls
`apm_connectors`' `POST /tools/actions/{id}/decision` — that decision
must come from a real authenticated human, outside this repo's control.
See `apm_connectors/docs/security-guardrails.md`.

## What's not here yet

- The avatar/voice/visual interface (Phase 2).
- Shared task/conversation state (Postgres) for the Supervisor to track
  multi-step plans across agents — each CLI invocation today is one
  independent turn.
- Any specialized agent besides Order-Renewal (Churn Prevention,
  Customer Onboarding, ...) — add one the same way: a new
  `agents/<name>/` package with its own `policy.yaml` + `agent.py`
  picking a toolbelt subset from `tools.py`, plus a new
  `delegate_to_<name>` tool in `supervisor.py`.

## Running locally

```bash
pip install -e ".[dev]"
cp .env.example .env   # fill in ANTHROPIC_API_KEY, APM_CONNECTORS_BASE_URL, etc.

# apm_connectors must already be running (see that repo's
# docs/running-locally.md), reachable at APM_CONNECTORS_BASE_URL.

apm-orchestrator "Acme Corp emailed asking to renew their annual license."
```

## Tests

```bash
pytest -q
```

Runs without live credentials or a running `apm_connectors` server —
`connectors_client` tests use `httpx.MockTransport`, and `policy` tests
just load the bundled YAML.
