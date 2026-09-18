"""Loads the Order-Renewal agent's policy from YAML (never hardcoded).

Keeping "what counts as a renewal", SLA windows, and blocking-ticket
rules as data means a policy change is a config edit, not a code
change -- and the next business agent (Churn Prevention, say) is a new
policy file + a different toolbelt subset, not new plumbing (see
docs/roadmap.md in apm_connectors).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_POLICY_PATH = Path(__file__).parent / "policy.yaml"
# Lets a real, org-specific policy.yaml (e.g. with real Jira project
# keys instead of the tracked file's REPLACE_WITH_... placeholders)
# live outside version control -- see .env.example. Previously only
# honored by agent.py's explicit policy_path param, never by
# case_graph.py's load_policy() calls -- this makes it work everywhere.
_POLICY_PATH_ENV_VAR = "ORDER_RENEWAL_POLICY_PATH"


@dataclass(frozen=True)
class OrderRenewalPolicy:
    raw: dict[str, Any] = field(repr=False)

    @property
    def gmail_query(self) -> str:
        return self.raw["detection"]["gmail_query"]

    @property
    def detection_max_results(self) -> int:
        return self.raw["detection"].get("max_results", 20)

    @property
    def salesforce_lookup_soql_template(self) -> str:
        return self.raw["salesforce"]["lookup_soql"]

    @property
    def salesforce_fallback_lookup_soql_template(self) -> str:
        return self.raw["salesforce"]["fallback_lookup_soql"]

    @property
    def renewal_window_days(self) -> int:
        return self.raw["sla"]["renewal_window_days"]

    @property
    def escalation_threshold_days(self) -> int:
        return self.raw["sla"]["escalation_threshold_days"]

    @property
    def blocking_jql_template(self) -> str:
        return self.raw["blocking_tickets"]["jql"]

    @property
    def blocking_issue_types(self) -> list[str]:
        return list(self.raw["blocking_tickets"].get("blocking_issue_types", []))

    @property
    def blocking_labels(self) -> list[str]:
        return list(self.raw["blocking_tickets"].get("blocking_labels", []))

    def route_for(self, account_tag: str | None) -> str:
        """Jira project key to route follow-up work to for this account."""
        routing = self.raw["follow_up_routing"]
        if account_tag:
            for route in routing.get("routes", []):
                if route["match"] == account_tag:
                    return route["project_key"]
        return routing["default_project_key"]

    @property
    def reporting_sheet(self) -> str:
        return self.raw["reporting"]["workbook_sheet"]

    @property
    def reporting_address(self) -> str:
        return self.raw["reporting"]["address"]

    @property
    def record_update_fields(self) -> dict[str, Any]:
        return dict(self.raw["record_update"]["fields_template"])


def load_policy(path: str | Path | None = None) -> OrderRenewalPolicy:
    policy_path = Path(path) if path else Path(os.environ.get(_POLICY_PATH_ENV_VAR) or _DEFAULT_POLICY_PATH)
    with policy_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return OrderRenewalPolicy(raw=raw)
