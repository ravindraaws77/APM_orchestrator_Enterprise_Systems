"""The durable, per-case Customer-Onboarding graph: detect -> verify ->
act -> record, as a LangGraph state machine checkpointed to Postgres --
one thread per case (`thread_id = case_id`), surviving process restarts.

Same two-graphs-for-two-jobs split as order_renewal (see that package's
case_graph.py module docstring): this is the deterministic,
poller-resumable skeleton; `agent.py`'s Claude Agent SDK loop is for
judgment that genuinely needs an LLM. Every node here is a fixed
apm_connectors call plus policy-driven Python logic (see `policy.py`),
never a fresh LLM tool-use loop.

Detection differs from order_renewal's: this agent's trigger is a
Salesforce state change (an Opportunity just closed as new business),
not an inbound Gmail signal -- see CUSTOMER_ONBOARDING_CONTRACT.md.
`account_name` is still a required input here, found by whatever starts
the case, same reasoning as order_renewal: every node stays a plain,
fast, unit-testable apm_connectors call.

Usage (see scripts/run_case.py):

    async with AsyncPostgresSaver.from_conn_string(database_url) as checkpointer:
        graph = build_case_graph(checkpointer)
        outcome = await start_case(graph, registry, "onboard-acme-2026-09-17", "Acme Corp")
        if not outcome.done:
            ...outcome.pending_action is now sitting in apm_connectors,
            waiting on a human; the poller resumes this case later...
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from apm_orchestrator.agents.customer_onboarding.policy import load_policy
from apm_orchestrator.connectors_client import ConnectorError
from apm_orchestrator.db import CaseRegistry
from apm_orchestrator.tools import get_client


class CaseState(TypedDict, total=False):
    case_id: str
    account_name: str
    raw_request: str

    opportunity: dict[str, Any] | None
    contact: dict[str, Any] | None
    blocked: bool
    blocker_reason: str | None
    kickoff_packet: dict[str, Any] | None
    call_start: str | None

    awaiting_step: str | None  # "kickoff_call" | "welcome_notice" | "onboarding_ticket" | "record_update"
    action_id: str | None  # RunOutcomeResponse.action_id -- NOT
    # pending_action["action_id"] (a different, internal state-store id --
    # see propose node comments below and order_renewal's own case_graph.py)
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


def _config(case_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": case_id}}


def _escape_soql_literal(value: str) -> str:
    """Escape a value going into a single-quoted SOQL string literal --
    SOQL uses a backslash before an embedded quote, same idea as SQL's
    doubled quote but with `\\'` instead."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _normalize_account_name(name: str) -> str:
    """Collapses the punctuation/whitespace/case differences a real
    account name and a caller's version of it commonly disagree on (a
    trailing period, extra whitespace, case) -- deliberately not fuzzy
    beyond that: two *different* accounts (a UK/Singapore subsidiary)
    should still normalize to different strings. Same rule as
    order_renewal's own helper -- CLAUDE.md's non-negotiable "never
    match a Salesforce account by exact string equality alone"."""
    return " ".join(name.split()).rstrip(".").casefold()


def _most_recently_closed(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The Opportunity most relevant to "a new customer to onboard right
    now" -- whichever CloseDate is the most recent, not just
    records[0]. Distinguishes a just-closed deal from an older
    already-onboarded Type='New Customer' Opportunity for the same
    account (an expansion deal, say). A record with no CloseDate sorts
    last."""

    def sort_key(record: dict[str, Any]) -> str:
        return record.get("fields", {}).get("CloseDate") or ""

    return max(records, key=sort_key)


def _next_business_day_slot(duration_minutes: int) -> tuple[datetime, datetime]:
    """A fixed slot on the next weekday -- there is no
    availability-checking route in apm_connectors' contract yet
    (calendar.search_events could rule out a double-booking, but
    choosing a genuinely free slot is future work, not this graph's job
    to fake). Same approach as order_renewal's own helper."""
    day = datetime.now(timezone.utc).date() + timedelta(days=1)
    while day.weekday() >= 5:  # Sat/Sun
        day += timedelta(days=1)
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=10)
    return start, start + timedelta(minutes=duration_minutes)


# -- Nodes ------------------------------------------------------------------


def _guarded(step_name: str):
    """Wraps a node so a real apm_connectors failure (unconfigured
    connector, network error, upstream 5xx) ends the case with a clear
    `stop_reason` instead of crashing the graph run -- same rationale as
    order_renewal's own decorator."""

    def decorator(fn):
        async def wrapped(state: CaseState) -> dict[str, Any]:
            try:
                return await fn(state)
            except ConnectorError as exc:
                steps = state.get("steps_completed", [])
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
    account_name = state["account_name"]
    escaped_name = _escape_soql_literal(account_name)

    soql = policy.salesforce_detect_soql_template.format(account_name=escaped_name)
    records = await client.salesforce_query_records(soql=soql)

    if not records:
        # Exact match found nothing -- widen the search, but tie-break by
        # normalized name rather than trusting every LIKE hit. Same
        # non-negotiable rule and reasoning as order_renewal's
        # verify_account_node: a trailing period/case difference
        # shouldn't stop a real onboarding, but a same-substring sibling
        # account shouldn't get silently treated as the right one either.
        fallback_soql = policy.salesforce_detect_fallback_soql_template.format(account_name=escaped_name)
        candidates = await client.salesforce_query_records(soql=fallback_soql)
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
                "steps_completed": steps + ["detect:ambiguous_account"],
            }
        if not matched_account_ids:
            return {
                "stop_reason": (
                    f"No new-business, closed-won Salesforce Opportunity found for {account_name!r}."
                ),
                "steps_completed": steps + ["detect:no_signal"],
            }

        matched_id = next(iter(matched_account_ids))
        records = [c for c in candidates if c["fields"].get("AccountId") == matched_id]

    opportunity = _most_recently_closed(records)
    close_date_str = opportunity.get("fields", {}).get("CloseDate")
    if close_date_str:
        days_since_close = (date.today() - date.fromisoformat(close_date_str)).days
        if abs(days_since_close) > policy.onboarding_window_days:
            return {
                "stop_reason": (
                    f"Opportunity {opportunity['record_id']} closed {abs(days_since_close)}d "
                    f"{'ago' if days_since_close > 0 else 'from now'}, outside the "
                    f"{policy.onboarding_window_days}d onboarding window."
                ),
                "steps_completed": steps + ["detect:outside_window"],
            }

    return {"opportunity": opportunity, "steps_completed": steps + ["detect"]}


@_guarded("verify_contact")
async def verify_contact_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])
    account_id = state["opportunity"]["fields"]["AccountId"]

    soql = policy.primary_contact_soql_template.format(account_id=_escape_soql_literal(account_id))
    contacts = await client.salesforce_query_records(soql=soql)
    contacts_with_email = [c for c in contacts if c.get("fields", {}).get("Email")]

    if not contacts_with_email:
        return {
            "stop_reason": f"No Salesforce Contact with an email address found for account {account_id!r}.",
            "steps_completed": steps + ["verify_contact:no_contact"],
        }

    return {"contact": contacts_with_email[0], "steps_completed": steps + ["verify_contact"]}


@_guarded("check_blockers")
async def check_blockers_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])

    jql = policy.blocking_jql_template.format(account_name=state["account_name"])
    issues = await client.jira_search_issues(jql=jql)
    for issue in issues:
        issue_type = issue.get("issue_type")
        labels = set(issue.get("fields", {}).get("labels", []) or [])
        if issue_type in policy.blocking_issue_types or labels & set(policy.blocking_labels):
            return {
                "blocked": True,
                "blocker_reason": f"{issue['issue_key']} ({issue_type})",
                "stop_reason": f"Onboarding already tracked by {issue['issue_key']} ({issue_type}).",
                "steps_completed": steps + ["check_blockers:blocked"],
            }

    return {"blocked": False, "steps_completed": steps + ["check_blockers"]}


async def pull_kickoff_packet_node(state: CaseState) -> dict[str, Any]:
    """Best-effort: never stops the case, since an existing kickoff
    packet template is useful context but not a hard prerequisite --
    same rationale as order_renewal's pull_contract_node."""
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])
    try:
        files = await client.drive_list_files(name_contains=policy.kickoff_packet_drive_name_contains)
    except ConnectorError:
        return {"steps_completed": steps + ["pull_kickoff_packet:unavailable"]}

    if not files:
        return {"steps_completed": steps + ["pull_kickoff_packet:not_found"]}
    return {"kickoff_packet": files[0], "steps_completed": steps + ["pull_kickoff_packet"]}


@_guarded("propose_kickoff_call")
async def propose_kickoff_call_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])
    start, end = _next_business_day_slot(policy.kickoff_call_duration_minutes)
    contact_email = state["contact"]["fields"]["Email"]

    result = await client.calendar_create_event(
        title=policy.kickoff_call_title_template.format(account_name=state["account_name"]),
        start=start.isoformat(),
        end=end.isoformat(),
        attendees=[contact_email],
    )
    return {
        # result["action_id"] is the id to poll/decide with -- NEVER
        # result["pending_action"]["action_id"], a different, internal
        # state-store id that happens to share the field name (see
        # order_renewal's case_graph.py and CLAUDE.md's non-negotiable rule).
        "action_id": result["action_id"],
        "pending_action": result["pending_action"],
        "awaiting_step": "kickoff_call",
        "call_start": start.isoformat(),
        "steps_completed": steps + ["propose_kickoff_call"],
    }


@_guarded("propose_welcome_notice")
async def propose_welcome_notice_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])
    contact_email = state["contact"]["fields"]["Email"]
    body = (
        f"Hi,\n\nWelcome aboard! We've scheduled a kickoff call on "
        f"{state['call_start']} to get your onboarding started.\n\n"
        f"Looking forward to connecting then.\n\nBest,\nAPM Onboarding Team"
    )
    result = await client.gmail_send_email(
        to=contact_email,
        subject=policy.welcome_notice_subject_template.format(account_name=state["account_name"]),
        body=body,
    )
    return {
        "action_id": result["action_id"],
        "pending_action": result["pending_action"],
        "awaiting_step": "welcome_notice",
        "steps_completed": steps + ["propose_welcome_notice"],
    }


@_guarded("propose_onboarding_ticket")
async def propose_onboarding_ticket_node(state: CaseState) -> dict[str, Any]:
    client = get_client()
    policy = load_policy()
    steps = state.get("steps_completed", [])

    result = await client.jira_create_issue(
        fields={
            "project": {"key": policy.onboarding_tracking_project_key},
            "summary": f"Onboarding: {state['account_name']}",
            "issuetype": {"name": policy.onboarding_tracking_issue_type},
        }
    )
    return {
        "action_id": result["action_id"],
        "pending_action": result["pending_action"],
        "awaiting_step": "onboarding_ticket",
        "steps_completed": steps + ["propose_onboarding_ticket"],
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
    else:
        summary = f"Onboarding workflow completed for {state['account_name']}."

    return {"final_summary": summary, "done": True}


# -- Routing ------------------------------------------------------------


def _route_after_check(state: CaseState) -> str:
    return "record_outcome" if state.get("stop_reason") else "_continue"


def _route_after_wait(state: CaseState) -> str:
    approved = bool((state.get("last_action_result") or {}).get("approved"))
    step = state.get("awaiting_step")
    if not approved:
        return "record_outcome"
    if step == "kickoff_call":
        return "propose_welcome_notice"
    if step == "welcome_notice":
        return "propose_onboarding_ticket"
    if step == "onboarding_ticket":
        return "propose_record_update"
    return "record_outcome"


def build_case_graph(checkpointer: Any):
    graph = StateGraph(CaseState)
    graph.add_node("detect", detect_node)
    graph.add_node("verify_contact", verify_contact_node)
    graph.add_node("check_blockers", check_blockers_node)
    graph.add_node("pull_kickoff_packet", pull_kickoff_packet_node)
    graph.add_node("propose_kickoff_call", propose_kickoff_call_node)
    graph.add_node("wait_for_approval", wait_for_approval_node)
    graph.add_node("propose_welcome_notice", propose_welcome_notice_node)
    graph.add_node("propose_onboarding_ticket", propose_onboarding_ticket_node)
    graph.add_node("propose_record_update", propose_record_update_node)
    graph.add_node("record_outcome", record_outcome_node)

    graph.set_entry_point("detect")
    graph.add_conditional_edges(
        "detect", _route_after_check, {"_continue": "verify_contact", "record_outcome": "record_outcome"}
    )
    graph.add_conditional_edges(
        "verify_contact",
        _route_after_check,
        {"_continue": "check_blockers", "record_outcome": "record_outcome"},
    )
    graph.add_conditional_edges(
        "check_blockers",
        _route_after_check,
        {"_continue": "pull_kickoff_packet", "record_outcome": "record_outcome"},
    )
    graph.add_edge("pull_kickoff_packet", "propose_kickoff_call")
    graph.add_conditional_edges(
        "propose_kickoff_call",
        _route_after_check,
        {"_continue": "wait_for_approval", "record_outcome": "record_outcome"},
    )
    graph.add_conditional_edges(
        "wait_for_approval",
        _route_after_wait,
        {
            "propose_welcome_notice": "propose_welcome_notice",
            "propose_onboarding_ticket": "propose_onboarding_ticket",
            "propose_record_update": "propose_record_update",
            "record_outcome": "record_outcome",
        },
    )
    graph.add_conditional_edges(
        "propose_welcome_notice",
        _route_after_check,
        {"_continue": "wait_for_approval", "record_outcome": "record_outcome"},
    )
    graph.add_conditional_edges(
        "propose_onboarding_ticket",
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
            action_id=payload["action_id"],
            pending_action=payload["pending_action"],
            step=payload["step"],
            final_summary=None,
            done=False,
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
) -> CaseOutcome:
    # Same live-verified guard as order_renewal's start_case: re-invoking
    # a case_id that already reached record_outcome (done=True) corrupts
    # the LangGraph checkpoint instead of cleanly restarting/resuming --
    # raise instead of letting that happen. See order_renewal/case_graph.py
    # and FAILURES_AND_LESSONS_LEARNED.md for the full root-cause writeup.
    existing = await graph.aget_state(_config(case_id))
    if existing.values.get("done"):
        raise ValueError(
            f"Case {case_id!r} already finished ({existing.values.get('final_summary')!r}). "
            "start_case never resumes or restarts a completed thread_id -- use a new case_id."
        )

    await registry.register(case_id, agent="customer_onboarding")
    initial: CaseState = {
        "case_id": case_id,
        "account_name": account_name,
        "raw_request": raw_request,
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
