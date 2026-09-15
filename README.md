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

## End-to-end smoke test (real server, no mocks)

`scripts/e2e_smoke.py` drives a real, running `apm_connectors` server
over real HTTP with this repo's actual `ConnectorsClient` — no mock
transport. It only needs the one connector that takes no external
credentials (local Excel), so it's runnable with zero live Gmail/
Salesforce/Jira accounts:

```bash
# In apm_connectors:
APM_EXCEL_WORKBOOK_PATH=/path/to/workbook.xlsx \
APM_API_KEYS="orchestrator:orch-key,human-approver:approver-key" \
uvicorn apm_connectors.api.app:app --port 8123

# In this repo:
python scripts/e2e_smoke.py \
    --base-url http://127.0.0.1:8123 \
    --orchestrator-key orch-key \
    --approver-key approver-key
```

Proves, against the real process: a read returns real workbook data; a
proposed write pauses with `final_result: null` and doesn't touch the
file; a second, differently-keyed caller approving it is what actually
writes the file; the audit trail attributes the propose and approve
events to those two different callers; and an unconfigured connector
(Salesforce here) 503s cleanly instead of crashing anything.

Running the Supervisor/Order-Renewal agent itself end to end
additionally needs `ANTHROPIC_API_KEY` set (see `.env.example`) — the
above only validates the HTTP boundary this repo's agents are built on.
