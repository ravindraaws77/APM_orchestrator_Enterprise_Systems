# Customer-Onboarding Agent — Contract and Implementation

The second business agent, per `apm_connectors`' `docs/roadmap.md`
("Order Renewal, Churn Prevention, Customer Onboarding, ..."). Started
as a bare-minimum design contract (`policy.yaml` +
this document) in a prior session; the three open questions below were
then reviewed and resolved, and `policy.py`, `case_graph.py`, `agent.py`,
`supervisor.py` wiring, and unit tests were implemented against the
resolved contract in this session. See "Status" below for exactly
what that does and doesn't cover.

## Status

**Implemented and unit-tested** (mocked `ConnectorsClient`, no live
credentials, no Postgres, no running `apm_connectors` server — same
style as `order_renewal`'s own `test_case_graph_nodes.py`):
- `policy.py` — the YAML loader.
- `case_graph.py` — `detect_node` → `verify_contact_node` →
  `check_blockers_node` → `pull_kickoff_packet_node` →
  `propose_kickoff_call_node` → `propose_welcome_notice_node` →
  `propose_onboarding_ticket_node` → `propose_record_update_node` →
  `record_outcome_node`, `build_case_graph()`, `start_case`/`resume_case`.
- `agent.py` — the Claude Agent SDK loop (`run_customer_onboarding`),
  mirroring `order_renewal/agent.py`.
- `supervisor.py` — a `delegate_to_customer_onboarding` tool, routed
  alongside `delegate_to_order_renewal`.
- `scripts/run_case.py` / `scripts/show_case.py` / `poller.py` — all
  three now take an `--agent`/dispatch on a per-case `agent` column to
  work with either business agent, not just `order_renewal`.
- `tests/test_customer_onboarding_policy.py` and
  `tests/test_customer_onboarding_case_graph_nodes.py` — 9 tests, all
  passing.

**Not done yet, by design:**
- **No live end-to-end run.** Nothing here has been exercised against a
  real Salesforce org, real Jira project, real Gmail/Calendar account,
  or a real Postgres checkpointer — order_renewal's own
  `test_case_graph_mechanics.py` (real Postgres + real server) has no
  Customer-Onboarding equivalent yet. Live-verifying this the way
  order_renewal's blocked path was live-verified (see
  `FAILURES_AND_LESSONS_LEARNED.md`) is the natural next step, and is
  likely to surface its own real-world surprises the same way that
  session did.
- The three placeholder/config items below still need real values
  before any of this can be trusted end to end.

## Why this shape

Per `docs/roadmap.md`'s own rule — *"agents are decomposed by business
process, not by connector"* — this agent gets its own policy and its
own subset of the existing 6 connectors, never a new connector. It
mirrors `order_renewal`'s proven shape (detect → verify → check
blockers → best-effort context pull → propose steps behind approval →
update the record of truth) because that shape is exactly what the
roadmap calls "generic enough … to be the template every later
business agent copies."

**Zero new connector work.** Every step below uses a method that
already exists, is already unit-tested, and (per the Coverage Map) is
mostly already live-verified:

| Step | Connector | Method | Live-verified elsewhere? |
|---|---|---|---|
| Detect a new customer | Salesforce | `query_records` | Yes (`verify_account_node`) |
| Verify the primary contact | Salesforce | `query_records` | Yes |
| Check for an existing onboarding ticket | Jira | `search_issues` | Yes (`check_blockers_node`) |
| Pull an existing welcome packet (best-effort) | Drive | `list_files`, `read_file` | **No** — flagged as a real gap in the Coverage Map |
| Propose a kickoff call | Calendar | `create_event` | Yes |
| Propose the welcome email | Gmail | `send_email` | Yes |
| Propose an onboarding tracking ticket | Jira | `create_issue` | **No** — closes the exact Jira-write gap the Coverage Map flagged as never wired into any graph |
| Update the Opportunity record | Salesforce | `update_record` | Yes |

Implementing this agent closes two of the concrete gaps `COVERAGE_MAP`
already identified (Drive read, Jira write) at the unit-test level —
`pull_kickoff_packet_node`/`propose_onboarding_ticket_node` now exercise
both, with mocked `ConnectorsClient` responses. Neither is
**live-verified** yet (see "Status"); that's the next real gap to close.

## Why detection is different from Order-Renewal

Order-Renewal detects from Gmail (an inbound email mentioning a
renewal) because a renewal conversation usually starts as a
conversation. Onboarding doesn't wait for an email — it starts the
moment a deal closes. Detecting it from a **Salesforce state change**
(`StageName = 'Closed Won' AND Type = 'New Business'`) is both more
natural and removes a dependency: this agent's `detect` step needs no
Gmail signal at all, only a SOQL query against data Order-Renewal
already proves this system can query reliably.

`Type = 'New Business'` is the discriminator that keeps this agent
from firing on every renewal Order-Renewal itself closes (which also
sets `StageName = 'Closed Won'`, per that agent's own
`record_update.fields_template`) — the two agents would otherwise
race on the exact same trigger condition.

**Resolved:** confirmed against the real Salesforce org that `Type`
does reliably distinguish `New Business` from `Renewal` on
Opportunity, so this stays the `detect` discriminator as originally
drafted.

**A gap the original draft had and this implementation closed:**
the draft's `detect` SOQL was exact-match-only
(`Account.Name = '{account_name}'`), which would have violated
`apm_orchestrator`'s own `CLAUDE.md` non-negotiable rule — "never match
a Salesforce account by exact string equality alone" — the exact
live-verified bug order_renewal's `verify_account_node` already hit and
fixed once (a trailing period silently returning zero rows). Before
writing `detect_node`, `policy.yaml` gained a `fallback_soql` (a
widened `LIKE` query) and `detect_node` now tie-breaks matches by
normalized-name equality, stopping on `ambiguous_account` if more than
one distinct `AccountId` matches — the identical pattern
`verify_account_node` uses, not a new one. See
`FAILURES_AND_LESSONS_LEARNED.md` for the full write-up of this catch.

Since a single account can have multiple New-Business Closed-Won
Opportunities over time (an expansion deal after the original one was
already onboarded), `detect_node` also doesn't trust `records[0]` —
it picks whichever match has the **most recent** `CloseDate`
(`_most_recently_closed`), the onboarding-specific analogue of
`verify_account_node`'s own `_closest_to_today` (that rule is
symmetric/forward-and-backward because a renewal window looks both
ways; this one only looks backward at what most recently closed).

## Resolved: where does onboarding status live?

**Question:** is `Onboarding_Status__c` real, could it just be
`Onboarding_Status`, and does the same field get updated again during
renewal later?

**Resolved, following the pattern `order_renewal`'s own
`record_update` already establishes:** `record_update` writes directly
to the **Opportunity** record `detect_node` found (`opportunity["record_id"]`),
never the Account — exactly how `order_renewal`'s `propose_record_update_node`
targets the Opportunity it verified, not the Account. Concretely:

- Salesforce **requires** every custom field's API name to end in
  `__c` — that's a platform rule, not a style choice, so the field
  stays `Onboarding_Status__c` in every SOQL/API call regardless of
  what it's labeled in the Salesforce UI ("Onboarding Status" is fine
  as a label).
- This agent sets `Onboarding_Status__c` on the **New-Business**
  Opportunity it detected, moving it to `"In Progress"` once the
  kickoff call and welcome email are both approved. It is never
  touched again after that by this agent or by `order_renewal`.
- **No, the same field is not reused or overwritten during renewal.**
  When it's time to renew, `order_renewal` detects and acts on a
  **different Opportunity record entirely** (`Type = 'Renewal'`,
  found by its own Gmail-driven `detect` + `verify_account` lookup by
  `Account.Name`) and writes its own field, `StageName = "Closed Won"`,
  on *that* record. The two agents never read each other's fields or
  share a record id — status is scoped per-deal, per-Opportunity,
  the same way `order_renewal`'s own status already is, not a single
  account-wide "customer lifecycle" field. (That would be a real,
  different design — a field on `Account` both agents write to — and
  wasn't asked for here.)

## What's a placeholder, not a real value

Same convention, same reasoning, as `order_renewal/policy.yaml`'s own
`REPLACE_WITH_...` markers (live-verified failure mode there:
a plausible-but-fictional Jira project key doesn't error, it silently
returns zero results forever).

**Resolved:** rather than introduce a new, onboarding-only placeholder,
`blocking_tickets.jql` and `onboarding_tracking.project_key` were
pointed at the exact same placeholder strings `order_renewal/policy.yaml`
already uses (`REPLACE_WITH_YOUR_SUPPORT_PROJECT_KEY` and
`REPLACE_WITH_YOUR_DEFAULT_JIRA_PROJECT_KEY` respectively) — this
portfolio runs both agents against one real Jira project, so a single
find-and-replace across both `policy.yaml` files fills in both agents
at once. Point either at a different real project instead if
onboarding ever needs its own.

## What's still not done

- **No live end-to-end run** against a real Salesforce/Jira/Gmail/
  Calendar/Postgres stack — see "Status" above. This is real, unproven
  risk: order_renewal's own live run surfaced several real bugs
  (the `action_id` disambiguation, the account-name-matching fallback,
  the case_id-reuse checkpoint corruption) that no amount of unit
  testing with a mocked client caught first. The same is likely true
  here.
- **`escalation_threshold_days` still isn't enforced in `case_graph.py`**
  — same as `order_renewal`'s own graph, which also only mentions it in
  `agent.py`'s system prompt, never in a node. Not a gap introduced
  here; a pre-existing scope boundary this agent's baseline matches.
- **`kickoff_packet` best-effort Drive pull never blocks the case** if
  no checklist template is found — by design, same as
  `order_renewal`'s `pull_contract_node`.

## Reference

- `src/apm_orchestrator/agents/order_renewal/` — the pattern every file
  here mirrors (`policy.yaml`+`policy.py`, `case_graph.py`, `agent.py`).
- `FAILURES_AND_LESSONS_LEARNED.md` — the account-matching non-negotiable
  rule this contract had to retrofit before implementation, and the
  case_id-reuse bug this agent's `start_case` guards against from day one.
- `COVERAGE_MAP` (Claude Artifact) — the gap analysis that motivated
  closing Drive-read and Jira-write coverage via this agent specifically.
- `apm_connectors/docs/roadmap.md` — "Agents are decomposed by
  business process, not by connector."
