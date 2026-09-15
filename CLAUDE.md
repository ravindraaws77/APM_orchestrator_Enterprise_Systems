# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this project is

The avatar + multi-agent reasoning layer for APM (Agentic Process
Management) — the Supervisor and every specialized business-process
agent (Order-Renewal today; Churn Prevention, Customer Onboarding, ...
later). See `README.md`.

Its sibling repo,
[`apm_connectors`](https://github.com/ravindraaws77/apm_connectors_enterprise_systems),
holds the connectors and the approval gate and stays reasoning-free —
see that repo's `CLAUDE.md` and `docs/roadmap.md` for why the split
exists and what belongs on which side of it.

## Non-negotiable rule

**No code in this repo may call `apm_connectors`' `POST
/tools/actions/{action_id}/decision` route.** That's the approval
boundary: a real authenticated human hitting that endpoint directly (or
through a human-facing review surface built for exactly that), never
something a Supervisor or specialized agent calls itself, regardless of
how sophisticated this repo's orchestration gets. `ConnectorsClient.
decide_action` exists only for that human-facing surface to use — never
wire it into an SDK tool any agent can call.

This repo talks to `apm_connectors` only over HTTP against its `/tools/*`
API (`src/apm_orchestrator/connectors_client.py`) — never by importing
that repo's internals.

**Never read `pending_action["action_id"]` as the id to poll or decide
with.** It's a different, internal `apm_connectors` state-store id that
happens to share a field name with the real one. The id that
`GET /processes/{id}/status` and `POST /tools/actions/{id}/decision`
both need is the response's top-level `action_id` -- see
`connectors_client.py`'s write methods' return shape and
`case_graph.py`'s propose nodes for the pattern (`state["action_id"] =
result["action_id"]`, never derived from `pending_action`). This was a
real bug live-verified against a running server; guarded by
`test_case_graph_nodes.py`'s happy-path test.

**Never match a Salesforce account by exact string equality alone.** A
real end-to-end run against a live org found `Account.Name =
'{account_name}'` silently returning zero rows because the real record
had a trailing period the inbound email's version of the name didn't.
`verify_account_node` now falls back to a `LIKE` query on exact-match
failure, but ties back to the account by *normalized*-name equality
(`_normalize_account_name`: collapse whitespace, strip a trailing
period, casefold) -- never by trusting every `LIKE` hit, since that also
matches unrelated sibling accounts (a UK/Singapore subsidiary whose name
contains the same substring). If normalized names match more than one
distinct `AccountId`, that's an `ambiguous_account` stop, not a guess.
See `tests/test_verify_account_matching.py`.

## Working conventions

- **Agents are decomposed by business process, not by connector.** A
  new specialized agent is a new `agents/<name>/` package (its own
  `policy.yaml` + `case_graph.py`), holding whatever subset of
  `src/apm_orchestrator/tools.py`'s tools that business process needs as
  its toolbelt — never a per-connector agent. Register its delegate tool
  in `supervisor.py`.
- **Policy is data, not code.** What counts as "a renewal" (or whatever
  the next agent's equivalent trigger is), SLA windows, and
  blocking-ticket rules belong in that agent's `policy.yaml`, loaded at
  run time — a policy change should never require a code change.
- **A business-process agent's durable state is a LangGraph case graph,
  not the Claude Agent SDK's own conversation loop.** `agent.py`
  (Claude Agent SDK, agentic tool-use) and `case_graph.py` (LangGraph,
  deterministic nodes + Postgres checkpointing) are two different tools
  for two different jobs -- see `case_graph.py`'s module docstring for
  when to reach for which, and for how its `wait_for_approval` interrupt
  relates to (and never shares state with) `apm_connectors`' own
  approval-gate graph.
- Never commit secrets. `.env.example` documents required variables;
  real values go in a local, gitignored `.env`.
- Adding a new `@tool` to `tools.py` is additive; changing an existing
  one's `input_schema` to match a breaking change in `apm_connectors`'
  own `/tools/*` contract needs both repos updated together.

## Layout

```
src/apm_orchestrator/
  config.py           env/config loading
  connectors_client.py  the only HTTP boundary to apm_connectors
  db.py                 CaseRegistry -- which case ids exist, for the poller
  tools.py             one Claude Agent SDK @tool per apm_connectors route
  supervisor.py         business-intent routing, delegates to agents
  poller.py             standalone process: resumes cases once their
                         apm_connectors action resolves (--once or --loop)
  agents/
    order_renewal/      pilot agent: policy.yaml + policy.py + agent.py
                         (Claude Agent SDK) + case_graph.py (LangGraph)
  cli.py               manual single-prompt smoke test (Supervisor)
scripts/
  run_case.py          manually start one durable case
  e2e_smoke.py          real-server smoke test, no mocks
tests/                 unit tests (mocked client, no infra) plus
                        test_case_graph_mechanics.py (real Postgres +
                        real server, skipped unless configured)
```
