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

**Live-verified end to end (2026-09-17)**, real Salesforce org + real
Jira site + real Gmail/Calendar account + real Neon Postgres, case
`onboard-initrode-1` / account `Initrode Corp`, all 4 approval steps
approved and executed for real:

| Step | Real result |
|---|---|
| `detect` → `verify_contact` → `check_blockers` → `pull_kickoff_packet` | Found Opportunity `006ak00000b9V0sAAE` (`Type = 'New Customer'`), Contact with a real email, no blockers, no kickoff packet (best-effort, correctly non-blocking) |
| `propose_kickoff_call` | Real Calendar event `tth7sjom9uv70hk4i4sbbalm7g` |
| `propose_welcome_notice` | Real Gmail send, `message_id: 1a0b103408765020` |
| `propose_onboarding_ticket` | Real Jira issue `KAN-10`, `issuetype: "Task"` |
| `propose_record_update` | Real Salesforce update: `Onboarding_Status__c: "In Progress"` on that same Opportunity |

Final state: `done: true`, `"Onboarding workflow completed for Initrode Corp."`

This run is exactly why the two live-test-prep fixes earlier in this
session mattered: `detect`'s SOQL used the real `Type = 'New Customer'`
value (not the fictional `'New Business'` originally drafted), and
`onboarding_tracking.issue_type` used the real `"Task"` (not the
fictional `"Onboarding"`) — either fictional value would have made
this run fail silently (a query matching nothing) or fail expensively
(a 400 on the very last approval, after three real writes already
executed). See `FAILURES_AND_LESSONS_LEARNED.md` section 8 for the
full write-up of both catches, and `apm_connectors`'
`docs/salesforce-jira-test-setup.md` for how to check a real org before
writing a policy value in the first place.

**Not done yet, by design:**
- **No mechanics-level test.** `order_renewal`'s own
  `test_case_graph_mechanics.py` (real Postgres, real interrupt/resume
  mechanics, skipped unless configured) has no Customer-Onboarding
  equivalent yet — this session's live run exercised the same mechanics
  manually (via `run_case.py`/`apm-orchestrator-poller`/`show_case.py`)
  but didn't turn it into an automated, repeatable test.
- **The blocked path (`check_blockers` actually blocking) wasn't
  exercised live** — only the happy path was. `order_renewal`'s own
  blocked-path test (KAN-9) has no Customer-Onboarding equivalent yet.
- **Rejection wasn't exercised live** — only approvals. The rejection
  path is unit-tested (`test_rejection_at_kickoff_call_stops_the_chain`)
  but not live-verified.

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
| Pull an existing welcome packet (best-effort) | Drive | `list_files`, `read_file` | **Partial** — the 2026-09-17 live run confirmed the *fallback* path (Drive unconfigured/no match → `pull_kickoff_packet:unavailable`, never blocks the case), but never actually exercised a successful Drive read against a real file, since no Drive folder was configured for that run |
| Propose a kickoff call | Calendar | `create_event` | Yes (live-verified 2026-09-17, event `tth7sjom9uv70hk4i4sbbalm7g`) |
| Propose the welcome email | Gmail | `send_email` | Yes (live-verified 2026-09-17, `message_id: 1a0b103408765020`) |
| Propose an onboarding tracking ticket | Jira | `create_issue` | Yes (live-verified 2026-09-17, `KAN-10`) — closes the exact Jira-write gap the Coverage Map flagged as never wired into any graph |
| Update the Opportunity record | Salesforce | `update_record` | Yes (live-verified 2026-09-17, `Onboarding_Status__c: "In Progress"`) |

Implementing this agent closes both concrete gaps `COVERAGE_MAP`
already identified: Jira write is now genuinely live-verified
end-to-end (`KAN-10`); Drive read's *fallback* behavior is live-verified,
but a real successful Drive read is still an open gap — set
`APM_DRIVE_FOLDER_ID` with a real "Onboarding Checklist"-named file in
it and re-run to close that one for real.

## Why detection is different from Order-Renewal

Order-Renewal detects from Gmail (an inbound email mentioning a
renewal) because a renewal conversation usually starts as a
conversation. Onboarding doesn't wait for an email — it starts the
moment a deal closes. Detecting it from a **Salesforce state change**
(`StageName = 'Closed Won' AND Type = 'New Customer'`) is both more
natural and removes a dependency: this agent's `detect` step needs no
Gmail signal at all, only a SOQL query against data Order-Renewal
already proves this system can query reliably.

`Type = '{value}'` is the discriminator that keeps this agent from
firing on every renewal Order-Renewal itself closes (which also sets
`StageName = 'Closed Won'`, per that agent's own
`record_update.fields_template`) — the two agents would otherwise race
on the exact same trigger condition.

**Corrected, not resolved as originally claimed:** this doc previously
said "confirmed against the real org that `Type` distinguishes `New
Business` from `Renewal`" based on a verbal answer, not an actual
check. Live-checking the real org's Opportunity `Type` dropdown during
live-test prep (screenshot review) found that assumption **wrong** —
this org has no `New Business`/`Renewal` values at all, only
Salesforce's plain standard set: `New Customer`, `Existing Customer -
Upgrade`, `Existing Customer - Replacement`, `Existing Customer -
Downgrade`. `Type = 'New Customer'` is the real-world equivalent: it
still safely distinguishes a fresh deal from a renewal/upgrade
Opportunity (which would be tagged `Existing Customer - *` instead),
so the discriminator concept survives, just not the literal string.
See `FAILURES_AND_LESSONS_LEARNED.md` for the full write-up — this is
exactly the kind of assumption a verbal "yes, that works" answer can't
actually verify; only checking the real picklist could.

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

## What used to be a placeholder, now a real value

Same convention, same reasoning, as `order_renewal/policy.yaml`'s own
`REPLACE_WITH_...` markers started out (live-verified failure mode
there: a plausible-but-fictional Jira project key doesn't error, it
silently returns zero results forever).

**Resolved, then resolved again:** rather than introduce a new,
onboarding-only placeholder, `blocking_tickets.jql` and
`onboarding_tracking.project_key` were pointed at the exact same
placeholder string `order_renewal/policy.yaml` already used — one
find-and-replace across both `policy.yaml` files would fill in both
agents at once. A later session tried keeping the real value out of
both tracked files entirely (a gitignored local-override YAML +
`*_POLICY_PATH` env vars), found that added more moving parts than it
was worth for a single-maintainer setup, and reverted it: both files
now carry the real project key (`KAN`) directly, with `SETUP` comments
marking it as the one thing to replace for a different Jira org. Point
either agent's `project_key`/`jql` at a different real project instead
if onboarding ever needs its own.

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
