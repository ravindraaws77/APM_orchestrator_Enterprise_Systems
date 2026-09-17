# Customer-Onboarding Agent — Baseline Contract

A bare-minimum design contract for the second business agent, per
`apm_connectors`' `docs/roadmap.md` ("Order Renewal, Churn Prevention,
Customer Onboarding, ..."). This document plus
`src/apm_orchestrator/agents/customer_onboarding/policy.yaml` are the
**contract** — the shape the next implementation session builds
`policy.py` and `case_graph.py` against. Nothing here runs yet; there
is deliberately no loader, no graph, no tests. That's the next step,
not this one.

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

Implementing this agent, even at a bare-minimum level, would close two
of the concrete gaps `COVERAGE_MAP` already identified (Drive read,
Jira write) — for free, without touching either connector.

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
race on the exact same trigger condition. **This assumes the
Salesforce org actually maintains an accurate `Type` picklist on
Opportunity.** If it doesn't, the first real implementation task is
finding a different, reliable "this is genuinely new business"
signal before wiring up `detect`, not inventing a workaround in code.

## What's a placeholder, not a real value

Same convention, same reasoning, as `order_renewal/policy.yaml`'s own
`REPLACE_WITH_...` markers (live-verified failure mode there:
a plausible-but-fictional Jira project key doesn't error, it silently
returns zero results forever). Both occurrences of
`REPLACE_WITH_YOUR_ONBOARDING_PROJECT_KEY` in this agent's
`policy.yaml` must be set to a real Jira project before the
`blocking_tickets` check or the `onboarding_tracking` proposal can be
trusted for anything.

## What this contract deliberately does not include

- **No `policy.py` loader.** `order_renewal/policy.py` is ~15 lines of
  dataclass properties reading `self.raw[...]` — the next session
  writes the equivalent for this shape once the fields above are
  confirmed, not before.
- **No `case_graph.py`.** No `detect_node`/`verify_contact_node`/etc.,
  no `build_case_graph()`, no wiring into `supervisor.py`.
- **No tests.** `order_renewal` has 6 test files covering its nodes,
  mechanics, and policy loading — this agent gets the same treatment
  once there's code to test.
- **No `escalation_threshold_days` handling**, `kickoff_packet`
  best-effort logic, or anything else beyond what a `policy.py`
  loader needs to expose — present in the YAML because the design
  calls for it, not because anything reads it yet.
- **No decision on where `Onboarding_Status__c` comes from.** This
  field name is a placeholder assumption, not a confirmed Salesforce
  custom field — verify it exists (or pick the real field) before
  `record_update` can mean anything.

## Reference

- `src/apm_orchestrator/agents/order_renewal/policy.yaml` +
  `policy.py` — the pattern this mirrors.
- `COVERAGE_MAP` (Claude Artifact) — the gap analysis that motivated
  closing Drive-read and Jira-write coverage via this agent specifically.
- `apm_connectors/docs/roadmap.md` — "Agents are decomposed by
  business process, not by connector."
