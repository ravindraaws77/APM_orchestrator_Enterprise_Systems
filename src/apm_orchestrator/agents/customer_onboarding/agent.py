"""The Customer-Onboarding agent: detect -> verify -> act -> record.

Same shape and rationale as order_renewal/agent.py -- this is the
Claude Agent SDK's own agentic tool-use loop, used by the Supervisor's
delegate tool for a synchronous conversational turn. The durable,
restart-safe version of this same workflow lives in `case_graph.py`
(started via scripts/run_case.py, resumed via the poller) -- see that
module's docstring and docs/roadmap.md's guidance on which tool is for
which job.
"""

from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from apm_orchestrator.agents.customer_onboarding.policy import CustomerOnboardingPolicy, load_policy
from apm_orchestrator.tools import apm_connectors_server

WORKFLOW_TOOLS = [
    "mcp__apm_connectors__salesforce_query_records",
    "mcp__apm_connectors__salesforce_get_record",
    "mcp__apm_connectors__jira_search_issues",
    "mcp__apm_connectors__jira_get_issue",
    "mcp__apm_connectors__drive_list_files",
    "mcp__apm_connectors__drive_read_file",
    "mcp__apm_connectors__calendar_create_event",
    "mcp__apm_connectors__gmail_send_email",
    "mcp__apm_connectors__jira_create_issue",
    "mcp__apm_connectors__salesforce_update_record",
]


def _render_system_prompt(policy: CustomerOnboardingPolicy) -> str:
    return f"""You are the Customer-Onboarding agent: detect a newly
closed-won new-business deal, verify the primary contact, act on it,
and record the outcome -- end to end, using only the tools you've been
given. Never invent data outside what a tool call returns.

Policy you must follow (read as data, not something to override):
- Detect a new customer with this SOQL template, filled in with the
  account name you were given: {policy.salesforce_detect_soql_template!r}
  If that returns nothing, retry with the widened fallback template:
  {policy.salesforce_detect_fallback_soql_template!r} -- and if more than
  one distinct Account matches once punctuation/case differences are
  ignored, stop and report the ambiguity rather than guessing.
- Onboarding is only actionable within {policy.onboarding_window_days}
  days of the Opportunity's CloseDate. Beyond
  {policy.escalation_threshold_days} days since close, treat it as
  at-risk: still propose your normal actions, but say clearly in your
  final summary that this needs expedited human attention.
- Look up the primary contact with this SOQL template, filled in with
  the Opportunity's AccountId: {policy.primary_contact_soql_template!r}.
  If no contact has an email address, stop -- there's no one to invite
  or write to.
- Before proposing anything, check whether onboarding is already
  tracked with this JQL template, filled in with the account name:
  {policy.blocking_jql_template!r}. A ticket blocks onboarding if its
  issue type is one of {policy.blocking_issue_types} or it carries any
  of these labels: {policy.blocking_labels}. If blocked, stop after
  reporting it -- do not propose the kickoff call, welcome email,
  tracking ticket, or record-update steps.

Workflow, in order, calling one tool at a time and reading its result
before deciding the next step:
1. Detect: salesforce_query_records to find the new-business, closed-won
   Opportunity per the policy above.
2. Verify: salesforce_query_records for the primary contact;
   jira_search_issues / jira_get_issue to check for blockers;
   drive_list_files / drive_read_file to pull an existing onboarding
   checklist/kickoff packet if one exists (best-effort -- never a
   blocker if missing).
3. Act (each of these only proposes a pending action -- nothing happens
   until a human approves it separately, and you cannot approve your
   own proposals):
   - calendar_create_event to propose a kickoff call with the contact.
   - gmail_send_email to propose the welcome email.
   - jira_create_issue (project {policy.onboarding_tracking_project_key!r},
     issue type {policy.onboarding_tracking_issue_type!r}) to propose an
     onboarding tracking ticket.
   - salesforce_update_record to propose marking the Opportunity's
     onboarding status ({policy.record_update_fields}) -- this writes to
     the SAME Opportunity record `detect` found, never the Account, and
     is never read or reused by the Order-Renewal agent's own renewal
     tracking (a separate Opportunity record with its own StageName).
4. Record: summarize what you detected, what you verified, and every
   pending action_id you proposed (or why you stopped early on a
   blocker), so a human reviewer knows exactly what's waiting on them."""


def _build_options(policy: CustomerOnboardingPolicy) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=_render_system_prompt(policy),
        mcp_servers={"apm_connectors": apm_connectors_server},
        # Listing a tool here is what auto-approves it at the SDK layer
        # (live-verified) -- see tools.py's module docstring.
        allowed_tools=WORKFLOW_TOOLS,
    )


async def run_customer_onboarding(prompt: str, *, policy_path: str | None = None) -> str:
    """Run one Customer-Onboarding turn and return its final text summary."""
    policy = load_policy(policy_path)
    options = _build_options(policy)

    final_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    final_text += block.text
        elif isinstance(message, ResultMessage) and message.subtype == "success":
            final_text = message.result or final_text
    return final_text
