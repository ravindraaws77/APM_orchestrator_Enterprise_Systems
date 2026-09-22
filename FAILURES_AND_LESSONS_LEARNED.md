# Failures and Lessons Learned

A record of every real failure hit during live end-to-end testing of the
Order-Renewal case across `apm_connectors` + `apm_orchestrator` (Windows,
a real Neon-hosted Postgres, a real Salesforce Developer org, a real
Gmail inbox, a real Jira project). Companion to the **Acme Renewal
Runbook**, which documents how to *run* the test; this document is about
everything that went wrong along the way, why, and what fixed it.

Each entry follows the same shape: **Symptom → Investigation → Root
cause → Fix → Takeaway**. The investigation steps are kept in, not
trimmed down to just the answer — the point of this document is as much
the debugging process as the individual bugs.

---

## 1. Environment / tooling failures

### 1.1 — `psql` not installed, and "local Postgres" was actually wrong

**Symptom:** `psql apm_connectors_dev -c "..."` failed with `'psql' is not
recognized as an internal or external command`.

**Investigation:**
- Checked for a local Postgres Windows service (`sc query | findstr
  /I postgres`) — nothing.
- Checked for a Docker container (`docker ps`) — nothing.
- Checked `netstat -ano | findstr :5432` — found active outbound
  connections to a **remote** IP, not `127.0.0.1`.

**Root cause:** There was no local Postgres server at all. Both
databases were hosted on **Neon** (two separate projects, one per repo,
both defaulting to the database name `neondb`) — a detail lost between
sessions, where an earlier summary had described the setup as "local
Postgres."

**Fix:** Installed just the `psql` client (via `winget`, deselecting
"PostgreSQL Server"/"Stack Builder" during install — no local server
needed for a hosted DB), then added its `bin` folder to `PATH`.

**Takeaway:** Don't trust an inherited description of an environment
("local Postgres") over what the machine actually shows you. `netstat`
+ `sc query` + `docker ps` took under a minute to find the real setup.

### 1.2 — cmd.exe silently truncates a connection string at `&`

**Symptom:** `set CONNECTORS_DB_URL=postgresql://...?sslmode=require&channel_binding=require`
typed unquoted in cmd.exe only assigned the part before `&` — cmd.exe
treats `&` as a command separator on the raw command line, including
inside a bare `set` assignment.

**Fix:** Quote the whole `NAME=VALUE` pair, not just the value:
`set "CONNECTORS_DB_URL=postgresql://...&channel_binding=require"`.

**Takeaway:** A classic shell-quoting gotcha, worth knowing cold:
cmd.exe's `&`/`|` interpretation is about where the *quote characters*
are on the line, not about what "looks" quoted.

### 1.3 — The real bug: a `postgresql://` URI argument breaks `-c` on Windows psql

**Symptom:** `psql "postgresql://user:pass@host/db?sslmode=require&channel_binding=require" -c "SELECT 1;"`
connected successfully (clean TLS handshake, correct database) but
**`-c` never executed** — psql dropped into an interactive prompt
instead, with `psql: warning: extra command-line argument "SELECT 1;"
ignored`.

**Investigation:**
1. First hypothesis: the `&` again, just this time confusing psql's own
   parsing rather than cmd.exe's. Tested by moving the URL into a
   `set "VAR=..."` variable first (eliminating any raw `&` on the
   command line at invocation time) — **same failure**. Ruled out `&`
   entirely.
2. Isolated further: dropped the URI positional argument completely and
   set individual `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`/
   `PGSSLMODE` environment variables instead, then ran bare `psql -c
   "SELECT 1;"` — **succeeded**, returned a real row.

**Root cause:** Combining a `postgresql://`-style positional connection
argument with `-c` on this Windows psql build (16.15) doesn't work —
confirmed reproducible, mechanism not fully explained, but cleanly
isolated to that specific combination.

**Fix:** Never pass a connection URI as a positional argument on
Windows when also using `-c`. Use per-variable `PGHOST`/`PGPASSWORD`/
etc. instead, switching `PGHOST`/`PGPASSWORD` to move between the two
repos' databases.

**Takeaway:** When two plausible causes are both in play (a shell
quoting issue *and* a client-side parsing issue), change **one variable
at a time** and re-test — moving the URL into a shell variable isolated
the shell from the equation in one step, and dropping the URI argument
entirely isolated the client's own argument parser in the next.

### 1.4 — Wrong column name: `apm_events.timestamp` doesn't exist

**Symptom:** `psql -c "SELECT ... FROM apm_events ORDER BY timestamp;"`
failed with `ERROR: column "timestamp" does not exist`.

**Root cause:** The Python code's JSON representation of an event
renames the column to `timestamp` for API consumers
(`postgres_store.py`'s `_event_row_to_dict`), but the actual SQL column
is `created_at`. Confirmed by reading `postgres_store.py`'s schema
directly rather than guessing again.

**Fix:** `ORDER BY created_at`, everywhere this query appeared (runbook,
quick-reference, verification checklist).

**Takeaway:** A one-line schema check (`_SCHEMA` in `postgres_store.py`)
resolved this immediately — faster than a third guess would have been.

---

## 2. Windows shell / invocation failures

- **`$(date +%s)` fails in both PowerShell and cmd.exe** — bash syntax,
  neither shell understands it. Fix: just use a literal string like
  `order-acme-1` for `case_id`; it never needed to be a timestamp.
- **`apm-orchestrator-poller --once` doesn't exist** — the module
  docstring documents `--once`, but the real `argparse` setup only
  defines `--loop`/`--interval`; running with **no flags** is the
  actual one-shot sweep. The docstring is stale relative to the code.
- **Running `run_case.py order-acme-2 "Acme Corp"` without the `python`
  prefix** fails with `'run_case.py' is not recognized...` — Windows
  doesn't execute `.py` files as commands on its own. Needs `python
  run_case.py ...`.
- **A "Select an app to open this .py file" dialog** appeared from
  double-clicking the script in an editor's file explorer — an
  unrelated Windows file-association prompt, not something triggered by
  or useful for running the script; dismiss it, don't pick an app.
- **`psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop'`**
  — Windows' default asyncio event loop isn't compatible with
  `psycopg`'s async mode. Fixed in this repo's own scripts via
  `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`
  on `sys.platform == "win32"`, before any asyncio-touching import --
  **but this guard doesn't travel with the file that needs it; it
  travels with whether that file touches Postgres, which can change
  later.** `cli.py` never needed it when written (no Postgres at all),
  then started calling `SupervisorRoutingLog.setup()` once routing-
  confidence logging landed and silently inherited this exact bug on
  Windows, live-reported by a real user running `apm-orchestrator` for
  the first time post-upgrade. Same gap hit the two new
  `scripts/review_routing_log.py`/`calibration_report.py` from day one.
  All three fixed the same way. The actual lesson: any `asyncio.run()`
  entry point that starts importing/calling into `db.py` needs this
  guard added in the same change, not caught later -- it's not a
  one-time repo-wide fix, it's a per-file obligation every new
  Postgres-touching entry point has to remember on its own.

- **Follow-up, same user, same command, immediately after the fix
  above shipped: a *second* Windows crash, from the fix itself.**
  With the `WindowsSelectorEventLoopPolicy` guard now in `cli.py`,
  `routing_log.setup()` succeeded -- but `run_supervisor()`'s call into
  the Claude Agent SDK (`query()`, which spawns the `claude` CLI as a
  subprocess) then failed with `NotImplementedError` from asyncio's
  `subprocess_exec`. **Root cause:** on Windows, subprocess creation
  only works under `ProactorEventLoop` -- `SelectorEventLoop` (what
  psycopg's async mode requires) can't create subprocesses at all. The
  original fix forced the *whole process* onto `SelectorEventLoop`,
  which happened to fix psycopg but silently broke the SDK subprocess
  in exchange -- two requirements that are mutually exclusive under one
  Windows event loop policy, discovered only because `cli.py` is the
  one entry point that needs both in the same run. **Fix:** rather than
  picking one loop policy for the whole process, `SupervisorRoutingLog`
  (`db.py`) now runs each of its psycopg calls to completion in a
  throwaway worker thread with its own private `SelectorEventLoop`
  (`_run_pg`/`_run_coro_on_selector_loop`) -- each call already opens a
  fresh, short-lived connection with no state living across calls, so
  this is safe. `cli.py`'s main loop goes back to Windows' default
  (`ProactorEventLoop`), so the SDK subprocess works again, while
  `SupervisorRoutingLog` transparently stays psycopg-safe regardless of
  the caller's loop. The `WindowsSelectorEventLoopPolicy` guards in
  `review_routing_log.py`/`calibration_report.py` became redundant once
  `db.py` handled this internally, and were removed. **Takeaway:** a fix
  validated only against the *original* symptom can still be wrong --
  this one was proven against a real Postgres connection and a full
  test suite, but nothing in that validation exercised the SDK
  subprocess path `cli.py` also depends on. The real regression test for
  a Windows event-loop fix in this repo is "does the *whole* entry
  point still work," not just the one call that crashed first.

---

## 3. Salesforce / test-data setup failures

- **"Stay Tuned… we are setting things up"** — caused by navigating to
  a placeholder example domain instead of the real org's domain (org
  ids are random, e.g. `orgfarm-3037d31428-dev-ed`). Always copy the
  domain from an already-logged-in browser tab.
- **The "+" Global Actions menu has no "New Account"** — it's a
  quick-create shortcut menu, not the App Launcher. Use the App
  Launcher (grid icon) or navigate directly to
  `<domain>/lightning/o/Account/new`.
- **Creating the test Opportunity as "Closed Won" by default** — that's
  the exact value the workflow's last step proposes setting it to;
  starting there makes that step invisible and can trip validation
  rules that block editing an already-closed Opportunity. Always create
  it in an open stage.

---

## 4. API-contract gotchas (apm_connectors)

- **`GET /processes/<id>/status` 404s immediately after proposing an
  action** — not a bug. `/status` reads a table only written once
  `execute_node` runs (i.e. after a decision); `/pending` is populated
  immediately at propose time and works right away.
- **Two different values are both called `action_id`** — the top-level
  one (what every `/status`/`/decision` URL actually needs) and a
  different, internal state-store id nested inside `pending_action`
  that happens to share the field name. This is real enough that it's
  now a non-negotiable rule in `apm_orchestrator`'s own `CLAUDE.md`, a
  live-verified bug, and guarded in code (`_disambiguate_pending_action`
  in `tools.py` strips the inner one so a tool result only ever has one
  field named `action_id`).

---

## 5. The big one: reusing a finished `case_id` corrupts state and throws a misleading error

This is the most involved investigation of the session, so it gets the
full write-up.

**Symptom:** Re-running `python run_case.py order-acme-2 "Acme Corp"` —
a `case_id` that had already finished successfully in an earlier run —
failed with:
```
Case order-acme-2 finished: detect failed: apm_connectors call to
/tools/gmail/search failed: All connection attempts failed
```
This looked exactly like a network/infrastructure problem: `apm_connectors`
unreachable, DNS/proxy misconfiguration, a Windows event-loop conflict,
or similar.

**Investigation (in the order actually run, each ruling out one variable):**

| # | Test | Result |
|---|------|--------|
| 1 | `curl http://127.0.0.1:8000/health` | ✅ succeeds |
| 2 | `curl -X POST http://127.0.0.1:8000/tools/gmail/search ...` | ✅ succeeds, real data |
| 3 | Checked `apm_orchestrator`'s `APM_CONNECTORS_BASE_URL` | Correct, matches default |
| 4 | Checked for `HTTP_PROXY`/`HTTPS_PROXY`/etc. env vars | None set |
| 5 | `python -c "import httpx; print(httpx.get(...))"` (sync, bare) | ✅ succeeds |
| 6 | Async `httpx.AsyncClient` + `asyncio.run()` (default `ProactorEventLoop`) | ✅ succeeds |
| 7 | Same, but with `WindowsSelectorEventLoopPolicy` set first (matching `run_case.py`) | ✅ succeeds |
| 8 | Same, but with a real `AsyncPostgresSaver` checkpointer connection held open concurrently | ✅ succeeds |
| 9 | Same, but also importing `apm_orchestrator.tools` (which triggers `claude_agent_sdk`'s `create_sdk_mcp_server()` at import time) | ✅ succeeds |
| 10 | A full, faithful copy of `run_case.py`'s logic (`CaseRegistry.setup()` → open checkpointer → `build_case_graph()` → `start_case()`) against a **fresh** `case_id` | ✅ succeeds — even reaches the correct blocked outcome |
| 11 | The **real, unmodified** `run_case.py` against a **fresh** `case_id` (`order-acme-3`) | ✅ succeeds |
| 12 | The **real, unmodified** `run_case.py` again against the **original, already-finished** `case_id` (`order-acme-2`) | ❌ fails, same error |

Every isolated variable — networking, proxies, event loop policy, an
open Postgres connection, the Claude Agent SDK import, even the full
graph machinery — checked out fine. The **only** variable that
correlated with the failure was reusing a `case_id` whose LangGraph
checkpoint had already reached a terminal state (`done: true`).

**A second, worse symptom, found while confirming this:** inspecting
the corrupted case afterward with `show_case.py order-acme-2` showed a
checkpoint that was internally self-contradictory:
```json
{
  "blocked": true,
  "blocker_reason": "KAN-9 (Task)",
  "steps_completed": ["detect"],
  "stop_reason": "detect failed: apm_connectors call to /tools/gmail/search failed: All connection attempts failed",
  "final_summary": "detect failed: ...",
  "done": true
}
```
`blocked`/`blocker_reason` are left over from the *original* successful
run; `stop_reason`/`final_summary`/the truncated `steps_completed` are
from the *new*, failed re-run. Both are present at once, as if they
happened in the same execution. This is because `CaseState` is a
`TypedDict(total=False)` with no custom reducers — each node's return
value only overwrites the specific keys it sets, so re-invoking with a
fresh `initial` state (which explicitly resets `steps_completed: []`)
merges on top of the *old* finished state instead of replacing it.

**Root cause:** Not fully isolated at the LangGraph-internals level —
*why* resuming/re-invoking a finished thread specifically manifests as
an `httpx` connection failure (rather than, say, a LangGraph-level
error) was never conclusively explained, despite the exhaustive
elimination above. What *is* conclusively established: `start_case`
was never designed to handle being called again for a `case_id` that
already reached `record_outcome`, and doing so is both unsafe
(checkpoint corruption) and confusing (a misleading error message).

**Fix:** `start_case` (in `case_graph.py`) now checks the existing
checkpoint via `graph.aget_state()` before invoking, and raises a clear,
honest error instead of silently corrupting the thread:
```python
existing = await graph.aget_state(_config(case_id))
if existing.values.get("done"):
    raise ValueError(
        f"Case {case_id!r} already finished ({existing.values.get('final_summary')!r}). "
        "start_case never resumes or restarts a completed thread_id -- use a new case_id."
    )
```
Live-verified fixed: the same `order-acme-2` re-run now raises
`ValueError: Case 'order-acme-2' already finished (...)` instead of the
misleading connection error, and no longer touches the checkpoint.
Shipped in [PR #2](https://github.com/ravindraaws77/APM_orchestrator_Enterprise_Systems/pull/2).

**Takeaways:**
- **An error message describing a plausible cause is not evidence of
  that cause.** "All connection attempts failed" pointed everywhere
  except the actual variable that mattered (checkpoint reuse). The fix
  was found only by systematically eliminating everything the message
  *seemed* to implicate.
- **Change one variable per test.** Twelve small, targeted tests beat
  guessing at the full system — each one either confirmed or ruled out
  exactly one thing.
- **A "fresh case_id always works" pattern is itself a strong clue.**
  Once noticed, it reframed the entire investigation from "what's wrong
  with the network/event loop/imports" to "what's different about a
  reused thread_id" — a much smaller, much more tractable question.
- **Durable state (a LangGraph checkpoint, a database row, anything
  keyed by an id meant to be created once) needs an explicit guard
  against being reused**, the same way you'd guard against a duplicate
  primary key — "the caller won't do that" is not a safe assumption for
  a script anyone can invoke by hand with any string.
- **A partial-update state model (no reducers, no reset) silently
  merges old and new state on reuse.** Worth knowing as a general
  LangGraph/state-machine pattern: `total=False` TypedDict state without
  reducers means every node's dict return is a *patch*, not a
  replacement — harmless for a single clean run, dangerous the moment
  the same thread is invoked twice.

---

## 6. Git workflow failures

- **`git pull origin main` refused with "untracked working tree files
  would be overwritten by merge"** for `scripts/show_case.py` — a local
  untracked draft of the same file PR #1 had already added upstream.
  Resolved by moving the local file aside (`show_case.py.local-bak`),
  pulling, then diffing the two to confirm nothing local-only was lost
  before deleting the backup. (It was purely an earlier, less-polished
  draft — no `DATABASE_URL` check, no friendly "no checkpoint found"
  error, `sys.argv` instead of `argparse`.)
- **A designated feature branch had already been merged mid-session**
  (PR #1, then later PR #2 on a second branch) — new work after a merge
  needs to continue from the merged history, never stack unrelated new
  commits on top of a branch whose only content is already-merged, and
  never assume a once-open PR is still the right place to keep pushing.

---

## 7. Customer-Onboarding implementation: a design-time near-miss, not a live bug

Unlike every entry above, this one wasn't caught by running anything —
Customer-Onboarding has no live end-to-end run yet (see
`CUSTOMER_ONBOARDING_CONTRACT.md`'s "Status" section). It's included
here anyway because it's the same *class* of mistake as #4 above, and
would have become a live one if it had shipped as originally drafted.

**What almost happened:** the baseline contract for this agent (drafted
in an earlier session, reviewed and implemented in this one) specified
`detect_node`'s Salesforce lookup as a single exact-match SOQL query:
`WHERE Account.Name = '{account_name}'`. That is *exactly* the shape of
query that already caused a real, live-verified failure in
`order_renewal`'s own `verify_account_node` (a trailing period in the
real account name silently returned zero rows) — the fix for which is
now `apm_orchestrator`'s own `CLAUDE.md` non-negotiable rule: never
match a Salesforce account by exact string equality alone.

**Root cause of the near-miss:** mirroring an existing agent's *shape*
(detect → verify → check blockers → propose → record) is not the same
as mirroring its *current, already-debugged implementation*. The
contract's own comparison table said this step was "Yes,
live-verified elsewhere" — true of the general approach, but the actual
SOQL template drafted alongside it had regressed to the exact-match-only
version that approach was built to move past.

**Fix:** before writing any code that depends on it, `policy.yaml`
gained a `fallback_soql` (widened `LIKE` query), and `detect_node` was
written from the start with the same normalized-name tie-break and
`ambiguous_account` stop condition as `verify_account_node` — not
retrofitted after a failure, because the non-negotiable rule was
checked against *before* implementing, not after something broke.

**Takeaway:** a documented non-negotiable rule earns its keep at the
moment a new module is drafted, not just when a bug report shows up
later. When copying an existing agent's proven shape for a new one,
diff the new draft against the *current* implementation of the piece
being mirrored — not just the general pattern description — since
that's exactly where an already-fixed bug can silently reappear.

---

## 8. Customer-Onboarding live-test prep: two assumptions that only broke once actually checked

Both found *before* running any code against real infra — during the
manual Salesforce/Jira setup walkthrough for this agent's live
end-to-end test — by looking at the real system instead of trusting
what the contract assumed. Neither is a code bug; both are the same
underlying lesson twice in one session.

### 8.1 — `onboarding_tracking.issue_type: "Onboarding"` isn't a real Jira issue type

**Symptom:** none yet — caught by inspecting a screenshot of the real
Jira board (`KAN` project) before running anything. The visible tickets
(`KAN-1`, `KAN-4`, `KAN-5`) were all typed as **Task**; nothing showed
an "Onboarding" type anywhere.

**Root cause:** the baseline contract picked `"Onboarding"` as a
plausible-sounding issue type without checking whether the real Jira
project actually has it configured. Standard Jira Kanban-template
projects only ship with Task/Bug/Story/Epic by default — "Onboarding"
was never real.

**Fix:** switched `blocking_tickets.blocking_issue_types` and
`onboarding_tracking.issue_type` to `"Task"` — the one type
live-checked to actually exist in this project, and the same fallback
`order_renewal`'s own `blocking_issue_types` already leans on for
exactly this reason (see that policy's own comment: "not every Jira
site uses a dedicated escalation type").

**Takeaway:** if this hadn't been caught here, `propose_onboarding_ticket_node`
would have gotten all the way through detect/verify/check-blockers/
kickoff-call/welcome-email, only to 400 on the *third* approval once a
human finally approved it — the most expensive possible point to
discover a made-up config value.

### 8.2 — `Type = 'New Business'` isn't a real Opportunity picklist value either

**Symptom:** none yet, same as above — caught by asking to see the
actual `Type` dropdown on a new Opportunity record before selecting
anything, rather than trusting a prior verbal confirmation.

**What the contract assumed:** that the real Salesforce org's
Opportunity `Type` field had `'New Business'` and `'Renewal'` values,
based on answering "yes, let's go with this approach" to a direct
question about it during contract review.

**What was actually there:** Salesforce's plain standard picklist —
`New Customer`, `Existing Customer - Upgrade`, `Existing Customer -
Replacement`, `Existing Customer - Downgrade`. No `New Business`, no
`Renewal`, ever.

**Fix:** `detect`'s SOQL (both the exact-match and fallback templates)
now filters on `Type = 'New Customer'` — the real value that serves the
same purpose (a fresh deal, distinct from any `Existing Customer - *`
renewal/upgrade Opportunity).

**Takeaway:** a verbal "yes, that's accurate" answer about a system's
configuration is not the same as checking the system. Both catches in
this section happened only because the live-test walkthrough asked
"show me the actual dropdown/board" instead of proceeding on a stated
assumption — the same discipline as this document's very first entry
(§1.1: "don't trust an inherited description of an environment over
what the machine actually shows you"), just applied to picklist values
and issue types instead of a database's location.

### 8.3 — Checking 8.1 for Customer-Onboarding surfaced the same bug already sitting in order_renewal

**Symptom:** none — found by checking the real Jira site's actual
configured work types (Space settings → Work types) as part of §8.1's
fix, then asking "does `order_renewal`'s own policy have the same
problem?" rather than assuming a bug fixed in one policy file couldn't
also be lurking in the sibling one.

**What was found:** this Jira site's real work types are exactly
**Epic, Story, Task, Subtask** — confirmed by URL-navigating straight to
`.../settings/issuetypes/...` rather than hunting through menus (a
`chellagurkir.atlassian.net`-specific UI that names things "Spaces"/
"Work types" instead of "Projects"/"Issue types", which cost a couple
of wasted turns before the direct URL worked). `order_renewal/policy.yaml`'s
`blocking_tickets.blocking_issue_types` was `["Escalation", "Bug",
"Task"]` — two of those three (`Escalation`, `Bug`) were never real on
this site either. Only `"Task"` ever actually matched anything, which
is exactly why the KAN-9 blocked-path test from the original live
session worked at all (that ticket was filed as a plain Task) — the
other two entries were silently inert the whole time, not caught
because they never needed to fire in that test.

**Fix:** trimmed to `blocking_issue_types: ["Task"]`, the one
live-checked value, with a comment pointing at how to verify a real
site's work types before ever adding another guess back.

**Takeaway:** an already-shipped, already-live-tested policy file is
not automatically clean just because its one tested path passed. A
JQL-matching list (or any "one of these values" check) can carry dead,
unverified entries indefinitely without failing anything — they only
get caught by explicitly checking the real system's configuration, not
by a passing test that happens to only exercise the one real value in
the list. Finding one instance of this mistake is a good moment to
grep for the same *shape* of mistake elsewhere in the codebase, not
just fix the instance in front of you.

---

## 9. Supervisor routing: live-verified for the first time, and it held up on an adversarial case

Unlike every entry above, this one isn't a bug — it's the closing of a
gap this document itself flagged: every real run so far (`order-acme-*`,
`onboard-initrode-1`) called each agent's `case_graph.py` directly via
`run_case.py --agent ...`, entirely bypassing `supervisor.py`. Its
system prompt claims Order-Renewal and Customer-Onboarding "never
overlap," and both delegate tools' descriptions warn against routing on
a mentioned connector/keyword instead of actual business intent — none
of that had ever been exercised against a real request.

**What was tested:** `scripts/test_supervisor_routing.py`, a new script
that calls `run_supervisor()`'s real code path (not `cli.py`, which
discards which tool got called) and traces which delegate tool, if any,
the Supervisor actually invokes. Three cases, run against the real
Claude API:

1. A clean Order-Renewal request ("Acme Corp's license is coming up for
   renewal... extend their existing contract").
2. A clean Customer-Onboarding request ("We just closed-won a brand-new
   deal with Initrode Corp -- kick off their onboarding").
3. A deliberately adversarial one: an *existing* customer (Order-Renewal
   territory by the system prompt's own definition) with an expansion
   deal, phrased using Customer-Onboarding's own vocabulary and proposed
   actions ("Schedule a kickoff call and send them a welcome email for
   the new product").

**Result:** cases 1 and 2 routed correctly. Case 3 — the one built to
tempt keyword-matching — routed to *neither* delegate. The Supervisor's
own words: *"I'm not routing this one, because it doesn't cleanly belong
to either specialist I have — and guessing would be worse than
stopping,"* followed by a specific breakdown of why Order-Renewal fits
the customer relationship but Customer-Onboarding fits the requested
actions, and why that conflict isn't its call to resolve unilaterally.

**Takeaway:** this is the first live evidence that `supervisor.py`'s
routing does what its system prompt and tool descriptions claim, on a
case built specifically to break that claim (existing-customer intent
paired with onboarding-shaped requested actions). It's also a reminder
that a documented behavioral claim ("never routes on keyword") is
exactly as untested as a documented bug fix until something actually
exercises the adversarial input — the same discipline as every other
entry in this document, just applied to a prompt-engineering claim
instead of a code path.

---

## 10. First live calibration batch run: two real bugs, then a clean 8/8

The first real exercise of the full Windows + calibration-tracking
stack together, on the same Windows machine section 2's fixes targeted
(`apm-orchestrator "Acme Corp's license is up for renewal..."`),
immediately after PRs #12/#13 merged. Three things happened, in order.

### 10.1 — The two Windows event-loop bugs, live-confirmed back-to-back

The user's first retry hit the exact `ProactorEventLoop`/psycopg crash
section 2 documents, fixed in #12. Pulling that fix and retrying
immediately hit a *second*, different crash from the fix itself: the
Claude Agent SDK's subprocess spawn (`query()` in `supervisor.py`, which
launches the `claude` CLI) needs `ProactorEventLoop`, but PR #12's fix
forces the whole process onto `SelectorEventLoop` to satisfy psycopg --
the two requirements are mutually exclusive under one Windows event loop
policy. Fixed in #13 by moving `SupervisorRoutingLog`'s psycopg calls
onto a private worker-thread selector loop (`db.py`'s `_run_pg`) instead
of picking a process-wide policy. See section 2's own entry for the full
detail -- noted here because this is where it was actually *proven*:
both bugs were caught back-to-back on the same real command, and the
fix for #13 was live-verified moments later by the calibration batch
run below completing cleanly.

### 10.2 — `run_calibration_batch.py` crashed on a real, boring cause: no API credits left

**Symptom:** `run_calibration_batch.py` crashed with
`claude_agent_sdk._errors.ResultError: Claude Code returned an error
result: Credit balance is too low`, as a bare traceback with no
indication of which of the 8 prompts had already been logged.

**Root cause:** not a bug -- the account backing `ANTHROPIC_API_KEY` had
run out of credits. Each delegated prompt runs the *full* downstream
agent (`mode="workflow"`), not just the routing decision, so a batch of
8 prompts is several real Claude API calls each, not 8 -- exactly the
cost this script's own docstring warns about, just hit for real the
first time it ran against a live account.

**Fix (PR #15):** topping up credits is the actual fix (nothing here is
code-fixable); what *is* fixable is that hitting this mid-batch
shouldn't cost the whole batch's progress. Added `--start N` (1-indexed)
plus a `try`/`except` around each prompt's `run_supervisor()` call that
prints exactly how many prompts logged successfully and the exact
`--start` value to resume from, instead of propagating a bare traceback.

**Takeaway:** the same discipline as every other entry here, applied to
an operational failure instead of a code bug -- the first time a script
actually runs against a live, finite resource (API credits, a rate
limit, a quota) is when its failure-partway-through behavior gets
tested for real, and "restart from scratch" is a bad default for
anything that costs real money per step.

### 10.3 — The full pipeline, working: 8/8 logged, 5 high / 3 low

Once credits were topped up, the first batch of 8 varied prompts
(`Wayne Enterprises`, `Sterling Cooper`, `Oscorp`, `Prestige Worldwide`,
`Tyrell Corp`, `Vandelay Industries`, `Dunder Mifflin`, and one
unnamed-account prompt -- `run_calibration_batch.py`'s `PROMPTS` has
since been refreshed to a second batch in PR #16, so this list won't
match the file as it stands today) ran clean end to end: every prompt
correctly delegated and logged to
`SupervisorRoutingLog`, 5 `high` / 3 `low`, each downstream agent run
correctly halting (no Salesforce record for a fictional company) rather
than erroring.

**One live finding worth flagging, not a bug:** the Vandelay Industries
prompt was written as an *adversarial* case -- correct routing
(Customer-Onboarding) is unambiguous once you read `SYSTEM_PROMPT`'s own
rule ("brand-new customer" decides it), the same shape as the other
adversarial cases in `evals/routing_cases.py`, which are all expected
`"high"`. It still routed correctly, but came back `"low"`. This is
exactly the kind of signal `calibration_report.py` exists to surface --
logged for human review (`review_routing_log.py`) rather than treated as
a routing bug, since the routing itself was correct.

**Takeaway:** this is the first time the Windows fixes, the calibration
logging path, and a real downstream agent run were all exercised
together in one live session -- and it took hitting two more real,
unrelated failures (a second Windows crash, an exhausted API account)
before reaching a clean run. Consistent with every other entry in this
document: nothing here was caught by review or by reasoning about the
code in the abstract, only by actually running it against a real
machine, a real account, and real (if fictional) prompts.

---

## Reference

- Runbook (execution guide, same test session): *Acme Renewal Runbook*
  (Claude Artifact).
- `scripts/test_supervisor_routing.py` — the live Supervisor-routing
  test behind §9 above.
- [PR #1](https://github.com/ravindraaws77/APM_orchestrator_Enterprise_Systems/pull/1) — `show_case.py`.
- [PR #2](https://github.com/ravindraaws77/APM_orchestrator_Enterprise_Systems/pull/2) — the `start_case` reused-`case_id` guard.
- `CLAUDE.md`'s non-negotiable rules — several of today's near-misses
  (the `action_id` disambiguation, the Salesforce account-matching
  logic, the Jira placeholder project keys) are bugs from *previous*
  sessions that are now permanently guarded in code specifically
  because they were this same kind of live-verified, easy-to-repeat
  mistake.
- `CUSTOMER_ONBOARDING_CONTRACT.md` — §7 above is this agent's own
  design-time catch of the same account-matching mistake, made before
  any code shipped rather than after a live run caught it.
