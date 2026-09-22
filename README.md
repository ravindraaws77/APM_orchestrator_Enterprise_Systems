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
  connector tool itself. Delegates to specialized agents, and requires a
  `confidence` ("high"/"low") plus a one-sentence `rationale` on every
  delegation — the answer to "a request that plausibly fits two agents,
  routes to the wrong one, and looks fine" is catching the ambiguity at
  decision time, not depending on someone noticing afterward. Optionally
  persisted to `SupervisorRoutingLog` (`db.py`) when `DATABASE_URL` is
  set; `scripts/show_low_confidence_routings.py` surfaces every
  low-confidence routing for review. Confidence only earns its keep if
  it's actually calibrated — `scripts/review_routing_log.py` samples
  both confidence buckets for a human verdict, and
  `scripts/calibration_report.py` reports whether "low" routings are
  actually wrong more often than "high" ones, from that reviewed
  ground truth.
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
- **Order-Renewal case graph** (`agents/order_renewal/case_graph.py`) —
  a durable LangGraph state machine, one Postgres-checkpointed thread
  per business case, sequencing detect → verify → check-blockers →
  (propose → wait-for-approval) × 3. Deliberately not agentic: every
  node is a fixed `apm_connectors` call plus policy logic, restart-safe
  and unit-testable without an LLM in the loop. This is a *different*
  use of LangGraph than `apm_connectors`' own `graph.py`, which gates a
  single write behind its own in-process approval `interrupt()` — see
  that module's docstring for how the two relate (they never share a
  checkpointer).
- **Poller** (`poller.py`) — a standalone process that resumes an
  in-flight case once its `apm_connectors` pending action resolves.
  Decoupled from everything else on purpose: run it once (`--once`, for
  cron/systemd-timer/CI schedules) or continuously (`--loop`).

**The approval boundary never moves.** No tool here ever calls
`apm_connectors`' `POST /tools/actions/{id}/decision` — that decision
must come from a real authenticated human, outside this repo's control.
See `apm_connectors/docs/security-guardrails.md`.

## What's not here yet

- The avatar/voice/visual interface (Phase 2).
- Any specialized agent besides Order-Renewal (Churn Prevention,
  Customer Onboarding, ...) — add one the same way: a new
  `agents/<name>/` package with its own `policy.yaml` + `case_graph.py`
  picking a toolbelt subset from `tools.py`, plus a new
  `delegate_to_<name>` tool in `supervisor.py`.
- The case graph (`case_graph.py`, LangGraph) hasn't been run against
  live Gmail/Calendar/Salesforce credentials yet -- only against an
  unconfigured server (a clean, graceful stop) and, for the
  interrupt/Postgres/poller mechanism specifically, a real server with
  Excel as a stand-in write (`tests/test_case_graph_mechanics.py`).
  The **Claude Agent SDK path** (`agent.py`, run via `apm-orchestrator`)
  *has* been run end to end against real Gmail/Salesforce/Jira -- see
  `CLAUDE.md`'s note on the exact-match SOQL bug that run surfaced and
  fixed in `verify_account_node`.

## Running locally

```bash
pip install -e ".[dev]"
cp .env.example .env   # fill in ANTHROPIC_API_KEY, APM_CONNECTORS_BASE_URL, etc.

# apm_connectors must already be running (see that repo's
# docs/running-locally.md), reachable at APM_CONNECTORS_BASE_URL.

apm-orchestrator "Acme Corp emailed asking to renew their annual license."
```

Before running against your own Salesforce/Jira org, edit each agent's
`policy.yaml` (`agents/order_renewal/policy.yaml`,
`agents/customer_onboarding/policy.yaml`) and replace every
`REPLACE_WITH_...` placeholder with a real value from your org (Jira
project keys especially -- see each file's `ACTION NEEDED` comments for
why a plausible-looking but wrong one fails silently or late instead of
at startup). These are the only per-org customization points; everything
else in `policy.yaml` is meant to be edited freely as your own business
rules change, per `CLAUDE.md`'s "policy is data, not code" convention.

If you're contributing changes back upstream, keep the `REPLACE_WITH_...`
placeholders in your commits -- both repos are public, and a real
project key belongs only in your own local checkout, not in a shared PR.

### Running a durable case (Postgres required)

```bash
pip install -e ".[postgres]"
# DATABASE_URL must be set (.env.example) -- a database this repo owns,
# separate from apm_connectors' own DATABASE_URL if it has one.

python scripts/run_case.py acme-2026-09-15 "Acme Corp"   # starts (or resumes-from-scratch) one case

# ... once a proposed action is approved/rejected in apm_connectors ...

apm-orchestrator-poller --once     # or --loop --interval 60 for a long-lived process
```

### Reviewing ambiguous Supervisor routings (Postgres required)

With `DATABASE_URL` set, every Supervisor run (`apm-orchestrator`, the
CLI in `cli.py`) logs its routing decision — delegate, confidence,
rationale — to `SupervisorRoutingLog`. Review the ones it flagged as a
close call:

```bash
python scripts/show_low_confidence_routings.py
```

`python scripts/test_supervisor_routing.py` (needs `ANTHROPIC_API_KEY`) —
a thin CLI shim over `apm_orchestrator.evals.run_supervisor_routing_eval`
— exercises this live against the real Claude API across the full
golden dataset (`evals/routing_cases.py`), checking each case's
confidence against what its category predicts (`"ambiguous"` cases
expected `"low"`, everything else expected `"high"`) alongside the
existing delegate pass/fail. `pytest tests/test_supervisor_routing_eval.py`
(same `ANTHROPIC_API_KEY` requirement) turns both into hard per-case
assertions.

**That eval check is not the same as calibration.** It only tells you
whether the model agrees with the dataset author's own labels — not
whether "low" confidence actually predicts a wrong routing on real
traffic. That's a different question, answered from reviewed ground
truth instead:

```bash
# Sample unreviewed routings from both confidence buckets and record a
# human verdict on each -- calibration needs to know the wrong-rate on
# BOTH sides, not just the ones already flagged as low-confidence.
python scripts/review_routing_log.py list --confidence low
python scripts/review_routing_log.py list --confidence high
python scripts/review_routing_log.py mark 42 --correct
python scripts/review_routing_log.py mark 43 --incorrect

# Once enough rows are reviewed (10+ per bucket), see whether "low"
# actually predicts wrong more often than "high" does:
python scripts/calibration_report.py
```

If the report comes back saying confidence isn't calibrated, that's a
real finding, not a bug to hide — it means `SYSTEM_PROMPT`'s calibration
guidance needs revisiting before the flagged-for-review queue can be
trusted.

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

## Case graph mechanism test (real Postgres + real server, no mocks)

`tests/test_case_graph_mechanics.py` proves the interrupt → Postgres
checkpoint → external resume mechanism `wait_for_approval` depends on,
using that exact function against a real `AsyncPostgresSaver` and a real
`apm_connectors` server (Excel again, as the one credential-free write --
see the file's docstring for why it doesn't drive the full Order-Renewal
graph). Skipped unless both `APM_TEST_DATABASE_URL` and a reachable
`APM_CONNECTORS_BASE_URL` are set:

```bash
pip install -e ".[dev,postgres]"
createdb apm_orchestrator_test   # or point at any disposable Postgres
APM_TEST_DATABASE_URL=postgresql://localhost/apm_orchestrator_test \
APM_CONNECTORS_BASE_URL=http://127.0.0.1:8123 \
APM_CONNECTORS_API_KEY=orch-key \
APM_APPROVER_API_KEY=approver-key \
pytest tests/test_case_graph_mechanics.py -v
```

`tests/test_case_graph_nodes.py` covers the Order-Renewal-specific node
logic (detect/verify/blockers routing, the propose→interrupt→resume
chain, and specifically the `action_id` vs. `pending_action["action_id"]`
distinction below) with a mocked client and `MemorySaver` -- no infra
needed at all.

**A real gotcha this repo's own tests caught**: a write route's
`RunOutcomeResponse.action_id` (top-level) and its
`pending_action["action_id"]` are *two different UUIDs* when no
`process_id` is passed to the propose call -- the former is
`apm_connectors`' process/thread id (what `GET /processes/{id}/status`
and `POST /tools/actions/{id}/decision` both need), the latter is an
internal state-store bookkeeping id that happens to share the field name
`action_id` inside the nested payload. `case_graph.py`'s propose nodes
store the top-level one explicitly (`state["action_id"]`) rather than
reading it back out of `pending_action` -- see the comment there before
"fixing" this the other way.
