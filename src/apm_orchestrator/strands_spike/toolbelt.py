"""The Strands toolbelt for renewal-readiness triage.

Four tools, built per run from a ``ConnectorsClient`` and the
Order-Renewal ``policy.yaml``. They differ from ``apm_orchestrator.tools``
(one generic tool per ``/tools/*`` route) on purpose: here the model
never writes SOQL or JQL. Each tool runs a query template from policy
and does the deterministic part (account matching, picking the
Opportunity closest to today, the renewal-window check, blocker
classification) in Python, reusing the Order-Renewal case graph's
live-verified helpers. The model only decides what to call and how to
summarise the result. That keeps the read side as reliable as the
LangGraph case graph, and leaves the agent's free choices to the parts
that need judgement.

Deliberately absent, same as ``apm_orchestrator.tools``: anything that
wraps ``POST /tools/actions/{action_id}/decision``. The only write tool,
``propose_follow_up_ticket``, returns a paused action that a human has
to approve against apm_connectors directly.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from strands import tool

from apm_orchestrator.agents.order_renewal.case_graph import (
    _closest_to_today,
    _escape_soql_literal,
    _find_stale_open_opportunities,
    _normalize_account_name,
)
from apm_orchestrator.agents.order_renewal.policy import OrderRenewalPolicy
from apm_orchestrator.connectors_client import ConnectorError, ConnectorsClient
from apm_orchestrator.tools import _disambiguate_pending_action

READ_TOOLS = frozenset(
    {"lookup_renewal_opportunity", "find_blocking_tickets", "check_action_status"}
)
PROPOSAL_TOOLS = frozenset({"propose_follow_up_ticket"})
TOOLBELT = READ_TOOLS | PROPOSAL_TOOLS


def _escape_jql_string(value: str) -> str:
    """Escape a value going into a double-quoted JQL string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_toolbelt(
    client: ConnectorsClient, policy: OrderRenewalPolicy, process_id: str | None = None
) -> list[Any]:
    """Returns the Strands tools for one triage run, bound to ``client``
    and ``policy``. ``process_id`` tags every connector call so the run
    shows up as one process in apm_connectors' audit log."""

    @tool
    async def lookup_renewal_opportunity(account_name: str) -> dict[str, Any]:
        """Find the Salesforce Opportunity relevant to a renewal for one account.

        Picks the Opportunity whose CloseDate is closest to today (past or
        future) and reports whether it is inside the renewal window. Also
        lists open Opportunities already past their close date. Read-only.

        Args:
            account_name: The customer account name, as written in the request.
        """
        escaped = _escape_soql_literal(account_name)
        try:
            records = await client.salesforce_query_records(
                soql=policy.salesforce_lookup_soql_template.format(account_name=escaped),
                process_id=process_id,
            )
            if not records:
                candidates = await client.salesforce_query_records(
                    soql=policy.salesforce_fallback_lookup_soql_template.format(account_name=escaped),
                    process_id=process_id,
                )
                target = _normalize_account_name(account_name)
                matched_ids = {
                    c["fields"].get("AccountId")
                    for c in candidates
                    if _normalize_account_name(c["fields"].get("Account", {}).get("Name") or "") == target
                }
                if len(matched_ids) > 1:
                    return {
                        "status": "ambiguous_account",
                        "detail": (
                            f"{len(matched_ids)} different Salesforce accounts match "
                            f"{account_name!r} once punctuation and case are ignored."
                        ),
                    }
                if not matched_ids:
                    return {"status": "not_found", "detail": f"No Opportunity found for {account_name!r}."}
                matched_id = next(iter(matched_ids))
                records = [c for c in candidates if c["fields"].get("AccountId") == matched_id]
        except ConnectorError as exc:
            return {"status": "error", "detail": str(exc)}

        opportunity = _closest_to_today(records)
        fields = opportunity.get("fields", {})
        close_date = fields.get("CloseDate")
        days_to_close = (date.fromisoformat(close_date) - date.today()).days if close_date else None
        return {
            "status": "found",
            "opportunity_id": opportunity.get("record_id"),
            "opportunity_name": fields.get("Name"),
            "stage": fields.get("StageName"),
            "amount": fields.get("Amount"),
            "close_date": close_date,
            "days_to_close": days_to_close,
            "in_renewal_window": (
                days_to_close is not None and abs(days_to_close) <= policy.renewal_window_days
            ),
            "renewal_window_days": policy.renewal_window_days,
            "escalation_threshold_days": policy.escalation_threshold_days,
            "stale_open_opportunities": _find_stale_open_opportunities(records),
        }

    @tool
    async def find_blocking_tickets(account_name: str) -> dict[str, Any]:
        """List open Jira tickets about this account and say which ones block the renewal.

        A ticket blocks if its work type or one of its labels is on the
        policy's blocking list. Read-only.

        Args:
            account_name: The customer account name, as written in the request.
        """
        jql = policy.blocking_jql_template.format(account_name=_escape_jql_string(account_name))
        try:
            issues = await client.jira_search_issues(jql=jql, process_id=process_id)
        except ConnectorError as exc:
            return {"status": "error", "detail": str(exc)}

        blocking_types = set(policy.blocking_issue_types)
        blocking_labels = set(policy.blocking_labels)
        tickets = []
        for issue in issues:
            labels = set(issue.get("fields", {}).get("labels", []) or [])
            tickets.append(
                {
                    "issue_key": issue.get("issue_key"),
                    "issue_type": issue.get("issue_type"),
                    "summary": issue.get("fields", {}).get("summary"),
                    "blocking": issue.get("issue_type") in blocking_types or bool(labels & blocking_labels),
                }
            )
        return {
            "status": "ok",
            "open_tickets": tickets,
            "blocking_ticket_keys": [t["issue_key"] for t in tickets if t["blocking"]],
        }

    @tool
    async def propose_follow_up_ticket(account_name: str, summary: str, description: str) -> dict[str, Any]:
        """Propose a Jira follow-up ticket for this account's renewal.

        This does NOT create anything. apm_connectors records it as a
        pending action that a human must approve. Report the returned
        top-level action_id to the reviewer. Call this at most once, and
        only when the renewal needs follow-up work (blockers or escalation).

        Args:
            account_name: The customer account name.
            summary: One-line ticket title.
            description: What needs to happen and why, citing the evidence found.
        """
        fields = {
            "project": {"key": policy.route_for(None)},
            "issuetype": {"name": policy.follow_up_issue_type},
            "summary": f"[Renewal] {account_name}: {summary}",
            "description": description,
        }
        try:
            result = await client.jira_create_issue(fields=fields, process_id=process_id)
        except ConnectorError as exc:
            return {"status": "error", "detail": str(exc)}
        return _disambiguate_pending_action(result)

    @tool
    async def check_action_status(action_id: str) -> dict[str, Any]:
        """Check whether a proposed action has been approved, rejected or is still pending. Read-only.

        Args:
            action_id: The top-level action_id returned by a proposal.
        """
        try:
            status = await client.get_action_status(action_id)
        except ConnectorError as exc:
            return {"status": "error", "detail": str(exc)}
        return status if status is not None else {"status": "pending_human_decision"}

    return [lookup_renewal_opportunity, find_blocking_tickets, propose_follow_up_ticket, check_action_status]
