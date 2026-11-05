"""The durable, per-case Order-Renewal graph: detect -> verify -> act ->
record, as a LangGraph state machine checkpointed to Postgres -- one
thread per case (`thread_id = case_id`), surviving process restarts.

This is a different, and differently-scoped, use of LangGraph than
`apm_connectors`' own `graph.py`. That one gates a single already-decided
write behind a human approval `interrupt()`, entirely inside that
repo's process. This one sequences an entire business case across
*several* such writes, calling `apm_connectors` over plain HTTP for
each -- its own `wait_for_approval` node interrupts *this* graph until
something external (the poller, see `poller.py`) observes that the
matching `apm_connectors` action resolved. The two interrupts are never
the same one; there is no shared checkpointer, and there never should
be (see CLAUDE.md's non-negotiable rule).

Deliberately not agentic: every node here is a fixed apm_connectors call
plus policy-driven Python logic (see `policy.py`), not a fresh LLM
tool-use loop. Judgment that genuinely needs an LLM (turning a customer
email into a drafted notice, say) belongs in `agent.py`'s Claude Agent
SDK agent -- this graph is the deterministic, restart-safe skeleton the
roadmap describes as "generic enough in shape ... to be the template
every later business agent copies." `account_name` is a required input
here (found by whatever starts the case -- a Supervisor classification
turn, a manual trigger) rather than extracted by a node, so every node
in this graph stays a plain, fast, unit-testable apm_connectors call.

Purchase-order intake (roadmap Phase 6.1 + 6.3) is an optional branch,
taken only when a case starts with `po_file_id` (a Drive file id):
`extract_po` reads the document through apm_connectors and sends it to
the document-extraction A2A agent (`document_extraction/`) -- the one
node here that calls something other than apm_connectors, and it calls
an agent, not an LLM. A low-confidence extraction pauses the case at
`wait_for_po_review` (a human checks the *values*; not the write
approval gate). `reconcile_po` then compares the PO with the
Opportunity using `po_reconciliation.py`: a match proposes the PO
fields onto the Opportunity before the renewal call; a mismatch
proposes a Jira ticket and ends the case. Both are ordinary
approval-gated writes.

Usage (see scripts/run_case.py):

    async with AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
        graph = build_case_graph(checkpointer)
        outcome = await start_case(graph, registry, "order-acme-2026-09-15", "Acme Corp")
        if not outcome.done:
            ...outcome.pending_action is now sitting in apm_connectors,
            waiting on a human; the poller resumes this case later...
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from apm_orchestrator.agents.order_renewal.po_reconciliation import normalize_account_name, reconcile
from apm_orchestrator.agents.order_renewal.policy import load_policy
from apm_orchestrator.connectors_client import ConnectorError
from apm_orchestrator.document_extraction.client import get_extraction_client
from apm_orchestrator.document_extraction.schema import parse_extraction
from apm_orchestrator.db import CaseRegistry
from apm_orchestrator.tools import get_client

logger = logging.getLogger("apm_orchestrator.case_graph")


class CaseState(TypedDict, total=False):
    case_id: str
    account_name: str
    raw_request: str

    signal_message: dict[str, Any] | None
    opportunity: dict[str, Any] | None
    stale_opportunities: list[str]  # past-due, still-open -- a data-hygiene
    # note surfaced alongside the outcome, never a reason to stop this case
    blocked: bool
    blocker_reason: str | None
    contract_doc: dict[str, Any] | None
    call_start: str | None

    # PO intake (optional -- only when the case starts with po_file_id)
    po_file_id: str | None
    po_document: dict[str, Any] | None  # {file_id, name, mime_type}
    po_task: dict[str, Any] | None  # ExtractionTaskResult.to_state()
    po_review_decision: dict[str, Any] | None
    po_reconciliation: dict[str, Any] | None

    awaiting_step: str | None  # "po_update" | "po_mismatch" | "call" | "notice" | "record_update"
    action_id: str | None  # the id to poll/decide -- RunOutcomeResponse.action_id,
    # NOT pending_action["action_id"] (a different, internal state-store id --
    # see propose node comments below)
    pending_action: dict[str, Any] | None
    last_action_result: dict[str, Any] | None

    steps_completed: list[str]
    stop_reason: str | None
    final_summary: str | None
    done: bool


@dataclass(frozen=True)
class CaseOutcome:
    """What start_case/resume_case hand back: either the graph is paused
    on an apm_connectors approval (pending_action set), or it's finished
    (done=True, final_summary set)."""

    case_id: str
    action_id: str | None
    pending_action: dict[str, Any] | None
    step: str | None
    final_summary: str | None
    done: bool
    # Set only while paused for a human to review extracted PO values
    # (step == "po_review"): the A2A task id, the extraction and why it
    # needs review.
    review: dict[str, Any] | None = None


def _config(case_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": case_id}}


def _escape_soql_literal(value: str) -> str:
    """Escape a value going into a single-quoted SOQL string literal --
    SOQL uses a backslash before an embedded quote, same idea as SQL's
    doubled quote but with `\\'` instead."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# One definition, shared with PO reconciliation (po_reconciliation.py).
_normalize_account_name = normalize_account_name


def _find_stale_open_opportunities(records: list[dict[str, Any]]) -> list[str]:
    """Opportunities past their close date that are still open -- a data-
    hygiene signal, never a reason to stop the current case (see
    record_outcome_node)."""
    today = date.today()
    stale = []
    for record in records:
        fields = record.get("fields", {})
        close_date_str = fields.get("CloseDate")
        if not close_date_str or fields.get("IsClosed"):
            continue
        if date.fromisoformat(close_date_str) < today:
            stale.append(f"{record['record_id']} ({fields.get('Name')}, due {close_date_str})")
    return stale


def _closest_to_today(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The Opportunity most relevant to a renewal check right now --
    whichever CloseDate is nearest today, past or future -- rather than
    trusting SOQL ordering to have surfaced the right one. A record with
    no CloseDate at all sorts last, never picked over one that has a
    date to reason about."""
    today = date.today()

    def distance(record: dict[str, Any]) -> float:
        close_date_str = record.get("fields", {}).get("CloseDate")
        if not close_date_str:
            return float("inf")
        return abs((date.fromisoformat(close_date_str) - today).days)

    return min(records, key=distance)


def _next_business_day_slot() -> tuple[datetime, datetime]:
    """A fixed 30-minute slot on the next weekday -- there is no
    availability-checking route in apm_connectors' contract yet
    (calendar.search_events could rule out a double-booking, but
    choosing a genuinely free slot is future work, not this graph's
    job to fake)."""
    day = datetime.now(timezone.utc).date() + timedelta(days=1)
    while day.weekday() >= 5:  # Sat/Sun
        day += timedelta(days=1)
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=10)
    return start, start + timedelta(minutes=30)


# -- Nodes ------------------------------------------------------------------


def _guarded(step_name: str):
    """Wraps a node so a real apm_connectors failure (unconfigured
    connector, network error, upstream 5xx) ends the case with a clear
    `stop_reason` instead of crashing the graph run -- these are
    real-world failures a durable, poller-driven system needs to survive
    cleanly, not exceptions to propagate."""

    def decorator(fn):
        async def wrapped(state: CaseState) -> dict[str, Any]:
            try:
                return await fn(state)
            except ConnectorError as exc:
                steps = state.get("steps_completed", [])
                logger.error(
                    "case %s: %s failed: %s", state.get("case_id"), step_name, exc
                )
                return {
                    "stop_reason": f"{step_name} failed: {exc}",
                    "steps_completed": steps + [f"{step_name}:error"],
                }

        return wrapped

    return decorator


@_guarded("detect")
async def detect_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])

    query = f'{policy.gmail_query} "{state["account_name"]}"'
    matches = await client.gmail_search_emails(
        query=query, max_results=policy.detection_max_results, process_id=state.get("case_id")
    )
    if not matches:
        return {
            "stop_reason": f"No renewal signal found in Gmail for {state['account_name']!r}.",
            "steps_completed": steps + ["detect:no_signal"],
        }

    message = await client.gmail_read_message(message_id=matches[0]["message_id"], process_id=state.get("case_id"))
    return {"signal_message": message, "steps_completed": steps + ["detect"]}


@_guarded("verify_account")
async def verify_account_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])
    account_name = state["account_name"]
    escaped_name = _escape_soql_literal(account_name)

    soql = policy.salesforce_lookup_soql_template.format(account_name=escaped_name)
    records = await client.salesforce_query_records(soql=soql, process_id=state.get("case_id"))

    if not records:
        # Exact match found nothing -- try a widened search, but tie-break
        # by normalized name rather than trusting every LIKE hit: a
        # trailing period or stray whitespace shouldn't stop a real
        # renewal, but a same-substring sibling account (a UK/Singapore
        # subsidiary) shouldn't get silently treated as the right one either.
        fallback_soql = policy.salesforce_fallback_lookup_soql_template.format(account_name=escaped_name)
        candidates = await client.salesforce_query_records(soql=fallback_soql, process_id=state.get("case_id"))
        target = _normalize_account_name(account_name)
        matched_account_ids = {
            candidate["fields"].get("AccountId")
            for candidate in candidates
            if _normalize_account_name(candidate["fields"].get("Account", {}).get("Name") or "") == target
        }

        if len(matched_account_ids) > 1:
            return {
                "stop_reason": (
                    f"{len(matched_account_ids)} different Salesforce accounts all match "
                    f"{account_name!r} once punctuation/case is ignored -- can't safely pick "
                    "one without a real Account id to disambiguate."
                ),
                "steps_completed": steps + ["verify_account:ambiguous_account"],
            }
        if not matched_account_ids:
            return {
                "stop_reason": f"No Salesforce Opportunity found for {account_name!r}.",
                "steps_completed": steps + ["verify_account:no_opportunity"],
            }

        matched_id = next(iter(matched_account_ids))
        records = [c for c in candidates if c["fields"].get("AccountId") == matched_id]

    stale = _find_stale_open_opportunities(records)
    opportunity = _closest_to_today(records)
    close_date_str = opportunity.get("fields", {}).get("CloseDate")
    if close_date_str:
        days_out = (date.fromisoformat(close_date_str) - date.today()).days
        # Symmetric: the closest-to-today Opportunity can be past or
        # future (see _closest_to_today), so "in window" means within
        # renewal_window_days either side of today, not just not-too-far
        # in the future.
        if abs(days_out) > policy.renewal_window_days:
            return {
                "stop_reason": (
                    f"Opportunity {opportunity['record_id']} closed/closes "
                    f"{abs(days_out)}d {'ago' if days_out < 0 else 'from now'}, "
                    f"outside the {policy.renewal_window_days}d renewal window."
                ),
                "stale_opportunities": stale,
                "steps_completed": steps + ["verify_account:outside_window"],
            }

    return {
        "opportunity": opportunity,
        "stale_opportunities": stale,
        "steps_completed": steps + ["verify_account"],
    }


@_guarded("check_blockers")
async def check_blockers_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])

    jql = policy.blocking_jql_template.format(account_name=state["account_name"])
    issues = await client.jira_search_issues(jql=jql, process_id=state.get("case_id"))
    for issue in issues:
        issue_type = issue.get("issue_type")
        labels = set(issue.get("fields", {}).get("labels", []) or [])
        if issue_type in policy.blocking_issue_types or labels & set(policy.blocking_labels):
            return {
                "blocked": True,
                "blocker_reason": f"{issue['issue_key']} ({issue_type})",
                "stop_reason": f"Blocked by open ticket {issue['issue_key']} ({issue_type}).",
                "steps_completed": steps + ["check_blockers:blocked"],
            }

    return {"blocked": False, "steps_completed": steps + ["check_blockers"]}


async def pull_contract_node(state: CaseState) -> dict[str, Any]:
    """Best-effort: never stops the case, since the existing contract is
    useful context but not a hard prerequisite for the rest of the flow."""
    client = get_client()
    steps = state.get("steps_completed", [])
    try:
        files = await client.drive_list_files(name_contains=state["account_name"], process_id=state.get("case_id"))
    except ConnectorError:
        return {"steps_completed": steps + ["pull_contract:unavailable"]}

    if not files:
        return {"steps_completed": steps + ["pull_contract:not_found"]}
    return {"contract_doc": files[0], "steps_completed": steps + ["pull_contract"]}


@_guarded("extract_po")
async def extract_po_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])

    doc = await client.drive_read_file(file_id=state["po_file_id"], process_id=state.get("case_id"))
    document = base64.b64decode(doc.get("content_base64") or "")
    result = await get_extraction_client().extract_purchase_order(
        document,
        media_type=doc.get("mime_type") or "application/octet-stream",
        filename=doc.get("name"),
        min_field_confidence=policy.po_min_field_confidence,
        required_fields=policy.po_required_fields,
    )
    update: dict[str, Any] = {
        "po_document": {"file_id": state["po_file_id"], "name": doc.get("name"), "mime_type": doc.get("mime_type")},
        "po_task": result.to_state(),
    }
    if result.state in ("failed", "rejected"):
        return {
            **update,
            "stop_reason": f"PO extraction {result.state} for {doc.get('name')!r}: {result.message}",
            "steps_completed": steps + [f"extract_po:{result.state}"],
        }
    suffix = ":needs_review" if result.state == "input_required" else ""
    return {**update, "steps_completed": steps + [f"extract_po{suffix}"]}


def wait_for_po_review_node(state: CaseState) -> dict[str, Any]:
    """Pauses this case until a human reviews the extracted PO values
    (scripts/review_po_extraction.py -> resume_po_review). Distinct from
    wait_for_approval: nothing is pending in apm_connectors here, so the
    poller leaves this interrupt alone."""
    decision = interrupt(
        {
            "type": "awaiting_po_field_review",
            "case_id": state["case_id"],
            "step": "po_review",
            "action_id": None,
            "pending_action": None,
            "review": state["po_task"],
        }
    )
    return {"po_review_decision": decision}


async def submit_po_review_node(state: CaseState) -> dict[str, Any]:
    steps = state.get("steps_completed", [])
    task = state["po_task"]
    decision = state["po_review_decision"] or {}
    result = await get_extraction_client().submit_review(
        task_id=task["task_id"],
        context_id=task["context_id"],
        reviewer=decision.get("reviewer") or "",
        decision=decision.get("decision") or "",
        reviewed_fields=decision.get("reviewed_fields"),
        reason=decision.get("reason"),
    )
    update: dict[str, Any] = {"po_task": result.to_state(), "po_review_decision": None}
    if result.state == "failed":
        return {
            **update,
            "stop_reason": f"PO review could not be applied: {result.message}",
            "steps_completed": steps + ["po_review:failed"],
        }
    # input_required again means the review itself was invalid (unknown
    # field, malformed value) -- the case goes back to waiting on a human.
    label = {"completed": "po_review", "rejected": "po_review:rejected"}.get(result.state, "po_review:invalid")
    return {**update, "steps_completed": steps + [label]}


def reconcile_po_node(state: CaseState) -> dict[str, Any]:
    policy = load_policy()
    steps = state.get("steps_completed", [])
    extraction = parse_extraction(state["po_task"]["extraction"])
    result = reconcile(
        extraction,
        state["opportunity"],
        state["account_name"],
        policy.po_reconciliation,
        policy.po_required_fields,
    )
    label = "reconcile_po:matched" if result.matched else "reconcile_po:mismatch"
    return {"po_reconciliation": result.to_state(), "steps_completed": steps + [label]}


@_guarded("propose_po_update")
async def propose_po_update_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])

    mapping = policy.po_record_fields
    placeholders = sorted(sf for sf in mapping.values() if sf.startswith("REPLACE_WITH_"))
    if placeholders:
        return {
            "stop_reason": (
                "PO reconciled, but policy.yaml's po_intake.record_fields still has placeholder "
                f"Salesforce field(s) {placeholders} -- set real Opportunity field API names "
                "before PO intake can propose an update."
            ),
            "steps_completed": steps + ["propose_po_update:unconfigured"],
        }

    extraction = parse_extraction(state["po_task"]["extraction"])
    fields = {sf: extraction.value(name) for name, sf in mapping.items() if extraction.value(name) is not None}
    result = await client.salesforce_update_record(
        object_name="Opportunity",
        record_id=state["opportunity"]["record_id"],
        fields=fields,
        process_id=state.get("case_id"),
    )
    return {
        "action_id": result["action_id"],
        "pending_action": result["pending_action"],
        "awaiting_step": "po_update",
        "steps_completed": steps + ["propose_po_update"],
    }


@_guarded("propose_po_mismatch_ticket")
async def propose_po_mismatch_ticket_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])
    task = state["po_task"]
    doc = state.get("po_document") or {}
    extraction = task.get("extraction") or {}
    po_number = ((extraction.get("fields") or {}).get("po_number") or {}).get("value")

    if task["state"] == "rejected":
        problems = [f"Rejected in human review: {task.get('message')}"]
    else:
        problems = (state.get("po_reconciliation") or {}).get("failures", [])
    lines = [
        f"A purchase order for {state['account_name']} did not reconcile with Opportunity "
        f"{state['opportunity']['record_id']} and needs a human to resolve it.",
        f"Document: {doc.get('name')} (Drive file {doc.get('file_id')})",
        f"A2A extraction task: {task.get('task_id')}",
        "",
        "Problems:",
        *[f"- {p}" for p in problems],
    ]
    result = await client.jira_create_issue(
        fields={
            "project": {"key": policy.po_mismatch_project_key},
            "summary": f"PO mismatch: {state['account_name']} ({po_number or 'no PO number'})",
            "issuetype": {"name": policy.po_mismatch_issue_type},
            "description": "\n".join(lines),
        },
        process_id=state.get("case_id"),
    )
    return {
        "action_id": result["action_id"],
        "pending_action": result["pending_action"],
        "awaiting_step": "po_mismatch",
        "steps_completed": steps + ["propose_po_mismatch_ticket"],
    }


@_guarded("propose_call")
async def propose_call_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    steps = state.get("steps_completed", [])
    start, end = _next_business_day_slot()

    result = await client.calendar_create_event(
        title=f"Renewal call: {state['account_name']}",
        start=start.isoformat(),
        end=end.isoformat(),
        process_id=state.get("case_id"),
    )
    return {
        # result["action_id"] (RunOutcomeResponse's top-level field, ==
        # the apm_connectors process_id/thread_id) is what
        # get_action_status/decide_action need -- NOT
        # result["pending_action"]["action_id"], which is a *different*,
        # internal state-store id apm_connectors happens to also call
        # "action_id" inside that nested payload (confirmed live: they
        # are two different UUIDs when no process_id is passed).
        "action_id": result["action_id"],
        "pending_action": result["pending_action"],
        "awaiting_step": "call",
        "call_start": start.isoformat(),
        "steps_completed": steps + ["propose_call"],
    }


@_guarded("propose_notice")
async def propose_notice_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    steps = state.get("steps_completed", [])
    to = state["signal_message"]["sender"]
    body = (
        f"Hi,\n\nThanks for reaching out about renewing your contract. "
        f"We've scheduled a call on {state['call_start']} to go over the "
        f"renewal -- looking forward to connecting then.\n\n"
        f"Best,\nAPM Renewals Team"
    )
    result = await client.gmail_send_email(
        to=to,
        subject=f"Renewal call scheduled - {state['account_name']}",
        body=body,
        process_id=state.get("case_id"),
    )
    return {
        "action_id": result["action_id"],
        "pending_action": result["pending_action"],
        "awaiting_step": "notice",
        "steps_completed": steps + ["propose_notice"],
    }


@_guarded("propose_record_update")
async def propose_record_update_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])
    opportunity = state["opportunity"]

    result = await client.salesforce_update_record(
        object_name="Opportunity",
        record_id=opportunity["record_id"],
        fields=policy.record_update_fields,
        process_id=state.get("case_id"),
    )
    return {
        "action_id": result["action_id"],
        "pending_action": result["pending_action"],
        "awaiting_step": "record_update",
        "steps_completed": steps + ["propose_record_update"],
    }


def wait_for_approval_node(state: CaseState) -> dict[str, Any]:
    """Pauses this graph -- not apm_connectors' -- until the poller
    observes the matching action_id resolved and calls resume_case.
    `interrupt()`'s return value is exactly the dict resume_case passes
    as `resume`."""
    decision = interrupt(
        {
            "type": "awaiting_apm_connectors_approval",
            "case_id": state["case_id"],
            "step": state["awaiting_step"],
            "action_id": state["action_id"],
            "pending_action": state["pending_action"],
        }
    )
    return {"last_action_result": decision, "pending_action": None}


def record_outcome_node(state: CaseState) -> dict[str, Any]:
    if state.get("stop_reason"):
        summary = state["stop_reason"]
    elif not (state.get("last_action_result") or {}).get("approved", True):
        summary = f"Stopped: the {state.get('awaiting_step')} step was rejected."
    elif state.get("awaiting_step") == "po_mismatch":
        summary = (
            f"PO for {state['account_name']} did not reconcile with the Opportunity -- routed to "
            "Jira; renewal paused until a human resolves it."
        )
    else:
        summary = f"Renewal workflow completed for {state['account_name']}."
        if (state.get("po_reconciliation") or {}).get("matched"):
            summary += " Purchase order reconciled and recorded on the Opportunity."

    stale = state.get("stale_opportunities")
    if stale:
        summary += (
            " Separately (outside this renewal's scope): these open Opportunities "
            f"are past their close date and likely need re-dating: {'; '.join(stale)}."
        )

    return {"final_summary": summary, "done": True}


# -- Routing ------------------------------------------------------------


def _route_after_check(state: CaseState) -> str:
    return "record_outcome" if state.get("stop_reason") else "_continue"


def _route_after_contract(state: CaseState) -> str:
    return "extract_po" if state.get("po_file_id") else "propose_call"


def _route_after_po_task(state: CaseState) -> str:
    if state.get("stop_reason"):
        return "record_outcome"
    return {
        "input_required": "wait_for_po_review",
        "completed": "reconcile_po",
        "rejected": "propose_po_mismatch_ticket",
    }.get(state["po_task"]["state"], "record_outcome")


def _route_after_reconcile(state: CaseState) -> str:
    return "propose_po_update" if state["po_reconciliation"]["matched"] else "propose_po_mismatch_ticket"


def _route_after_wait(state: CaseState) -> str:
    approved = bool((state.get("last_action_result") or {}).get("approved"))
    step = state.get("awaiting_step")
    if not approved:
        return "record_outcome"
    if step == "po_update":
        return "propose_call"
    if step == "call":
        return "propose_notice"
    if step == "notice":
        return "propose_record_update"
    return "record_outcome"


def build_case_graph(checkpointer: Any):
    graph = StateGraph(CaseState)
    graph.add_node("detect", detect_node)
    graph.add_node("verify_account", verify_account_node)
    graph.add_node("check_blockers", check_blockers_node)
    graph.add_node("pull_contract", pull_contract_node)
    graph.add_node("extract_po", extract_po_node)
    graph.add_node("wait_for_po_review", wait_for_po_review_node)
    graph.add_node("submit_po_review", submit_po_review_node)
    graph.add_node("reconcile_po", reconcile_po_node)
    graph.add_node("propose_po_update", propose_po_update_node)
    graph.add_node("propose_po_mismatch_ticket", propose_po_mismatch_ticket_node)
    graph.add_node("propose_call", propose_call_node)
    graph.add_node("wait_for_approval", wait_for_approval_node)
    graph.add_node("propose_notice", propose_notice_node)
    graph.add_node("propose_record_update", propose_record_update_node)
    graph.add_node("record_outcome", record_outcome_node)

    graph.set_entry_point("detect")
    graph.add_conditional_edges(
        "detect", _route_after_check, {"_continue": "verify_account", "record_outcome": "record_outcome"}
    )
    graph.add_conditional_edges(
        "verify_account",
        _route_after_check,
        {"_continue": "check_blockers", "record_outcome": "record_outcome"},
    )
    graph.add_conditional_edges(
        "check_blockers",
        _route_after_check,
        {"_continue": "pull_contract", "record_outcome": "record_outcome"},
    )
    graph.add_conditional_edges(
        "pull_contract", _route_after_contract, {"extract_po": "extract_po", "propose_call": "propose_call"}
    )
    po_task_routes = {
        "wait_for_po_review": "wait_for_po_review",
        "reconcile_po": "reconcile_po",
        "propose_po_mismatch_ticket": "propose_po_mismatch_ticket",
        "record_outcome": "record_outcome",
    }
    graph.add_conditional_edges("extract_po", _route_after_po_task, po_task_routes)
    graph.add_edge("wait_for_po_review", "submit_po_review")
    graph.add_conditional_edges("submit_po_review", _route_after_po_task, po_task_routes)
    graph.add_conditional_edges(
        "reconcile_po",
        _route_after_reconcile,
        {"propose_po_update": "propose_po_update", "propose_po_mismatch_ticket": "propose_po_mismatch_ticket"},
    )
    for node in ("propose_po_update", "propose_po_mismatch_ticket"):
        graph.add_conditional_edges(
            node, _route_after_check, {"_continue": "wait_for_approval", "record_outcome": "record_outcome"}
        )
    graph.add_conditional_edges(
        "propose_call",
        _route_after_check,
        {"_continue": "wait_for_approval", "record_outcome": "record_outcome"},
    )
    graph.add_conditional_edges(
        "wait_for_approval",
        _route_after_wait,
        {
            "propose_call": "propose_call",
            "propose_notice": "propose_notice",
            "propose_record_update": "propose_record_update",
            "record_outcome": "record_outcome",
        },
    )
    graph.add_conditional_edges(
        "propose_notice",
        _route_after_check,
        {"_continue": "wait_for_approval", "record_outcome": "record_outcome"},
    )
    graph.add_conditional_edges(
        "propose_record_update",
        _route_after_check,
        {"_continue": "wait_for_approval", "record_outcome": "record_outcome"},
    )
    graph.add_edge("record_outcome", END)

    return graph.compile(checkpointer=checkpointer)


# -- Entry points -------------------------------------------------------


def _to_outcome(case_id: str, result: dict[str, Any]) -> CaseOutcome:
    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        return CaseOutcome(
            case_id=case_id,
            action_id=payload.get("action_id"),
            pending_action=payload.get("pending_action"),
            step=payload["step"],
            final_summary=None,
            done=False,
            review=payload.get("review"),
        )
    return CaseOutcome(
        case_id=case_id,
        action_id=None,
        pending_action=None,
        step=None,
        final_summary=result.get("final_summary"),
        done=True,
    )


async def start_case(
    graph: Any,
    registry: CaseRegistry,
    case_id: str,
    account_name: str,
    raw_request: str = "",
    po_file_id: str | None = None,
) -> CaseOutcome:
    # Re-invoking a case_id that already reached record_outcome
    # (done=True) is a real, live-verified failure mode on Windows:
    # ainvoke() doesn't cleanly restart or resume that thread -- it goes
    # on to raise a genuine httpx connection error out of detect_node's
    # very first apm_connectors call ("All connection attempts failed"),
    # even though nothing about networking or config actually changed
    # (the identical call always succeeds against a fresh case_id; every
    # narrower isolated repro -- plain httpx, httpx alongside an open
    # AsyncPostgresSaver connection, importing apm_orchestrator.tools,
    # even a from-scratch build_case_graph()+start_case() call -- only
    # failed once it reused an already-finished thread_id). Root cause
    # not fully isolated; guard with an honest error instead of that
    # misleading message.
    existing = await graph.aget_state(_config(case_id))
    if existing.values.get("done"):
        raise ValueError(
            f"Case {case_id!r} already finished ({existing.values.get('final_summary')!r}). "
            "start_case never resumes or restarts a completed thread_id -- use a new case_id."
        )

    await registry.register(case_id, agent="order_renewal")
    initial: CaseState = {
        "case_id": case_id,
        "account_name": account_name,
        "raw_request": raw_request,
        "po_file_id": po_file_id,
        "steps_completed": [],
    }
    result = await graph.ainvoke(initial, config=_config(case_id))
    return _to_outcome(case_id, result)


async def resume_case(
    graph: Any, case_id: str, approved: bool, final_result: dict[str, Any] | None = None
) -> CaseOutcome:
    result = await graph.ainvoke(
        Command(resume={"approved": approved, "final_result": final_result}),
        config=_config(case_id),
    )
    return _to_outcome(case_id, result)


async def resume_po_review(
    graph: Any,
    case_id: str,
    reviewer: str,
    decision: str,
    reviewed_fields: dict[str, Any] | None = None,
    reason: str | None = None,
) -> CaseOutcome:
    """Resume a case paused at wait_for_po_review with a human's review of
    the extracted PO values: decision "accept" (optionally with corrected
    `reviewed_fields`) or "reject". Refuses to resume a case that isn't
    actually waiting on a PO review, so this can never be used to push a
    case past an apm_connectors approval."""
    snapshot = await graph.aget_state(_config(case_id))
    payloads = [i.value for i in snapshot.interrupts]
    if not payloads or payloads[0].get("type") != "awaiting_po_field_review":
        raise ValueError(f"Case {case_id!r} is not waiting on a PO review.")
    if decision not in ("accept", "reject"):
        raise ValueError('decision must be "accept" or "reject"')
    result = await graph.ainvoke(
        Command(
            resume={
                "reviewer": reviewer,
                "decision": decision,
                "reviewed_fields": dict(reviewed_fields or {}),
                "reason": reason,
            }
        ),
        config=_config(case_id),
    )
    return _to_outcome(case_id, result)
