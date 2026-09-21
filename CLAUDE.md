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

**Every Jira project key in `policy.yaml` is a placeholder you must
replace, never a working default.** Live-verified twice: a plausible-
looking but fictional project key (`OPS`, `SUPPORT`, `ENTSUCCESS`, ...)
doesn't fail at startup -- a *write* route (`jira_create_issue`) 400s
only once a real proposal reaches it, and a *read* route
(`jira_search_issues` for blocking tickets) is worse: it just returns
zero results forever, silently turning a safety check into a permanent
false "no blockers." The shipped policy.yaml now spells every one of
these as `REPLACE_WITH_...` specifically so it can't be mistaken for a
real value left unedited. Never reintroduce a plausible-sounding
placeholder project key (an "OPS"-shaped guess) in its place.

**Never pick "the" Opportunity for a renewal check with `records[0]`
after an `ORDER BY`.** Live-verified: `ORDER BY CloseDate ASC LIMIT 5`
on a long-lived account grabs the *oldest* handful of Opportunities
ever, which can silently exclude the one actually relevant today. Both
SOQL templates in `policy.yaml` fetch a wide candidate set with no
`ORDER BY` reliance; `verify_account_node` picks whichever `CloseDate`
is closest to today (past or future) via `_closest_to_today`, and the
window check (`abs(days_out) > renewal_window_days`) is symmetric for
the same reason -- a recently-passed close date is still "in window,"
not just an upcoming one. See
`test_picks_opportunity_closest_to_today_not_the_ascending_first`.

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
- **Never hardcode a picklist value, issue type, or project key into a
  `policy.yaml` without checking it against the real Salesforce/Jira
  instance first.** A plausible-sounding one doesn't error — it just
  silently matches nothing (a SOQL/JQL filter) or 400s only once a
  write is finally approved (`jira_create_issue`'s `issuetype`), the
  most expensive point to find out. Two real examples that shipped
  before being checked: `Type = 'New Business'` (the real org only has
  `New Customer`/`Existing Customer - *`) and `issue_type: "Onboarding"`
  (the real Jira project only has Epic/Story/Task/Subtask) — see
  `FAILURES_AND_LESSONS_LEARNED.md`'s live-test-prep section for both,
  and `apm_connectors`' `docs/salesforce-jira-test-setup.md` for how to
  actually check.
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
- **A wrong-but-plausible Supervisor routing needs to be caught at
  decision time, not reconstructed afterward.** The adversarial case (a
  request matching no specialized agent) was already covered by a clean
  decline; the harder case is a request that plausibly fits *two*
  agents, routes to one of them, and comes back looking perfectly fine —
  nobody files a bug for that. Every delegate tool in `supervisor.py`
  requires `confidence` ("high"/"low") and a one-sentence `rationale`;
  `run_supervisor`'s optional `routing_log` persists this to
  `SupervisorRoutingLog` (`db.py`), and
  `scripts/show_low_confidence_routings.py` surfaces every routing the
  Supervisor itself flagged as a close call. A new delegate tool must
  keep these two fields required, not optional — an ambiguous routing
  with no confidence signal is exactly the silent-failure case this
  exists to prevent.

## Layout

```
src/apm_orchestrator/
  config.py           env/config loading
  connectors_client.py  the only HTTP boundary to apm_connectors
  db.py                 CaseRegistry (which case ids exist, for the poller)
                         + SupervisorRoutingLog (routing confidence, for review)
  tools.py             one Claude Agent SDK @tool per apm_connectors route
  supervisor.py         business-intent routing, delegates to agents,
                         requires confidence/rationale on every delegation
  poller.py             standalone process: resumes cases once their
                         apm_connectors action resolves (--once or --loop)
  agents/
    order_renewal/      pilot agent: policy.yaml + policy.py + agent.py
                         (Claude Agent SDK) + case_graph.py (LangGraph)
  evals/                golden-dataset evals against the real Claude API
                         (routing_cases.py + run_supervisor_routing_eval.py) --
                         distinct from tests/: judged by pass-rate over a
                         versioned dataset, not assert-equal-on-one-input
  cli.py               manual single-prompt smoke test (Supervisor)
scripts/
  run_case.py          manually start one durable case
  show_case.py         print one case's current/final checkpoint state
  show_low_confidence_routings.py  review routings the Supervisor flagged as a close call
  test_supervisor_routing.py       live routing test, asserts confidence too
  e2e_smoke.py          real-server smoke test, no mocks
  test_supervisor_routing.py  thin CLI shim over evals/run_supervisor_routing_eval.py
tests/                 unit tests (mocked client, no infra) plus
                        test_case_graph_mechanics.py (real Postgres +
                        real server, skipped unless configured) and
                        test_supervisor_routing_eval.py (real Claude API,
                        skipped unless ANTHROPIC_API_KEY is set -- see
                        .github/workflows/eval.yml for the scheduled CI run)
```
