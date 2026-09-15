"""The Order-Renewal agent: detect -> verify -> act -> record.

The pilot business-process agent from apm_connectors' docs/roadmap.md --
generic enough in shape to be the template every later business agent
(Churn Prevention, Customer Onboarding, ...) copies. It owns one
end-to-end business capability and holds a scoped subset of
apm_connectors' tools as its own toolbelt, per that doc's "Agents are
decomposed by business process, not by connector".

Two toolbelts, not one, matching the roadmap's own separation:
- WORKFLOW_TOOLS: the live detect -> verify -> act -> record loop.
- REPORTING_TOOLS: the periodic Excel report generated *from* Salesforce
  state -- "separate cadence, not the live workflow" (roadmap), and
  Salesforce stays the system of record either way.
"""

from __future__ import annotations

from typing import Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from apm_orchestrator.agents.order_renewal.policy import OrderRenewalPolicy, load_policy
from apm_orchestrator.tools import apm_connectors_server

Mode = Literal["workflow", "report"]

WORKFLOW_TOOLS = [
    "mcp__apm_connectors__gmail_search_emails",
    "mcp__apm_connectors__gmail_read_message",
    "mcp__apm_connectors__salesforce_query_records",
    "mcp__apm_connectors__salesforce_get_record",
    "mcp__apm_connectors__jira_search_issues",
    "mcp__apm_connectors__jira_get_issue",
    "mcp__apm_connectors__drive_list_files",
    "mcp__apm_connectors__drive_read_file",
    "mcp__apm_connectors__calendar_create_event",
    "mcp__apm_connectors__gmail_send_email",
    "mcp__apm_connectors__drive_upload_file",
    "mcp__apm_connectors__drive_update_file",
    "mcp__apm_connectors__jira_create_issue",
    "mcp__apm_connectors__salesforce_update_record",
]

REPORTING_TOOLS = [
    "mcp__apm_connectors__salesforce_query_records",
    "mcp__apm_connectors__excel_write_range",
]


def _render_system_prompt(mode: Mode, policy: OrderRenewalPolicy) -> str:
    if mode == "report":
        return f"""You generate the periodic Order-Renewal report. This is a
separate, lower-stakes cadence from the live renewal workflow -- you
only read Salesforce (the system of record) and propose one
`excel_write_range` call to sheet "{policy.reporting_sheet}" at address
"{policy.reporting_address}". Never treat the Excel workbook as a
second place to write live order/renewal state.

Every write you propose only records a pending action in apm_connectors
-- nothing is written until a human approves it separately. You have no
ability to approve your own proposals."""

    return f"""You are the Order-Renewal agent: detect a renewal signal,
verify it, act on it, and record the outcome -- end to end, using only
the tools you've been given. Never invent data outside what a tool call
returns.

Policy you must follow (read as data, not something to override):
- Detect signals with this Gmail query: {policy.gmail_query!r}
  (max_results: {policy.detection_max_results})
- Look up the account's Opportunity in Salesforce using this SOQL
  template, filled in with the account name/domain you found:
  {policy.salesforce_lookup_soql_template!r}
- A renewal is only actionable within {policy.renewal_window_days} days
  of the Opportunity's CloseDate. Inside
  {policy.escalation_threshold_days} days of it, treat the renewal as
  at-risk: still propose your normal actions, but say clearly in your
  final summary that this needs expedited human attention.
- Before proposing the renewal call, notice, or record update, check for
  blocking tickets with this JQL template (filled in with the account
  name): {policy.blocking_jql_template!r}. A ticket blocks the renewal
  if its issue type is one of {policy.blocking_issue_types} or it carries
  any of these labels: {policy.blocking_labels}. If blocked, stop after
  reporting the blocker -- do not propose the renewal-call, send,
  document, or record-update steps.
- If follow-up work needs to be routed to another team/partner, target
  a real Jira project via `salesforce`/account context: use
  `policy.route_for(account_tag)`-equivalent judgment -- ask yourself
  which of these tags the account matches, then use that project key
  (default project key if none match): {policy.raw['follow_up_routing']}

Workflow, in order, calling one tool at a time and reading its result
before deciding the next step:
1. Detect: gmail_search_emails, then gmail_read_message on the best
   match(es).
2. Verify: salesforce_query_records / salesforce_get_record to find the
   account and its Opportunity; jira_search_issues / jira_get_issue to
   check for blockers per the policy above; drive_list_files /
   drive_read_file to pull the existing contract/agreement if relevant.
3. Act (each of these only proposes a pending action -- nothing happens
   until a human approves it separately, and you cannot approve your
   own proposals):
   - calendar_create_event to propose a renewal call.
   - gmail_send_email to propose the renewal notice/confirmation.
   - drive_upload_file or drive_update_file to propose storing the
     finalized renewal document.
   - jira_create_issue to propose routing any follow-up work.
   - salesforce_update_record to propose updating the Opportunity's
     stage/close date -- Salesforce is the system of record, not Excel.
4. Record: summarize what you detected, what you verified, and every
   pending action_id you proposed (or why you stopped early on a
   blocker), so a human reviewer knows exactly what's waiting on them."""


def _build_options(mode: Mode, policy: OrderRenewalPolicy) -> ClaudeAgentOptions:
    tools = WORKFLOW_TOOLS if mode == "workflow" else REPORTING_TOOLS
    return ClaudeAgentOptions(
        system_prompt=_render_system_prompt(mode, policy),
        mcp_servers={"apm_connectors": apm_connectors_server},
        allowed_tools=tools,
        # Safe to auto-approve at the SDK layer: every "write" tool above
        # only proposes a pending action in apm_connectors: the real gate
        # is that service's own human-approval endpoint, never something
        # this SDK session can reach.
        permission_mode="bypassPermissions",
    )


async def run_order_renewal(
    prompt: str,
    *,
    mode: Mode = "workflow",
    policy_path: str | None = None,
) -> str:
    """Run one Order-Renewal turn and return its final text summary."""
    policy = load_policy(policy_path)
    options = _build_options(mode, policy)

    final_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    final_text += block.text
        elif isinstance(message, ResultMessage) and message.subtype == "success":
            final_text = message.result or final_text
    return final_text
