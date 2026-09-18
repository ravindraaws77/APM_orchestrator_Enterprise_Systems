"""Loads the Customer-Onboarding agent's policy from YAML (never
hardcoded). Same rationale and shape as order_renewal/policy.py: what
counts as "a new customer", SLA windows, and blocking-ticket rules stay
data, so a policy change is a config edit, not a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_POLICY_PATH = Path(__file__).parent / "policy.yaml"
# Lets a real, org-specific policy.yaml (e.g. with a real Jira project
# key instead of the tracked file's REPLACE_WITH_... placeholder) live
# outside version control -- see .env.example.
_POLICY_PATH_ENV_VAR = "CUSTOMER_ONBOARDING_POLICY_PATH"


@dataclass(frozen=True)
class CustomerOnboardingPolicy:
    raw: dict[str, Any] = field(repr=False)

    @property
    def salesforce_detect_soql_template(self) -> str:
        return self.raw["detection"]["salesforce_new_customer_soql"]

    @property
    def salesforce_detect_fallback_soql_template(self) -> str:
        return self.raw["detection"]["fallback_soql"]

    @property
    def detection_max_results(self) -> int:
        return self.raw["detection"].get("max_results", 20)

    @property
    def onboarding_window_days(self) -> int:
        return self.raw["sla"]["onboarding_window_days"]

    @property
    def escalation_threshold_days(self) -> int:
        return self.raw["sla"]["escalation_threshold_days"]

    @property
    def primary_contact_soql_template(self) -> str:
        return self.raw["salesforce"]["primary_contact_soql"]

    @property
    def blocking_jql_template(self) -> str:
        return self.raw["blocking_tickets"]["jql"]

    @property
    def blocking_issue_types(self) -> list[str]:
        return list(self.raw["blocking_tickets"].get("blocking_issue_types", []))

    @property
    def blocking_labels(self) -> list[str]:
        return list(self.raw["blocking_tickets"].get("blocking_labels", []))

    @property
    def kickoff_packet_drive_name_contains(self) -> str:
        return self.raw["kickoff_packet"]["drive_name_contains"]

    @property
    def kickoff_call_title_template(self) -> str:
        return self.raw["kickoff_call"]["title_template"]

    @property
    def kickoff_call_duration_minutes(self) -> int:
        return self.raw["kickoff_call"]["duration_minutes"]

    @property
    def welcome_notice_subject_template(self) -> str:
        return self.raw["welcome_notice"]["subject_template"]

    @property
    def onboarding_tracking_project_key(self) -> str:
        return self.raw["onboarding_tracking"]["project_key"]

    @property
    def onboarding_tracking_issue_type(self) -> str:
        return self.raw["onboarding_tracking"]["issue_type"]

    @property
    def record_update_fields(self) -> dict[str, Any]:
        return dict(self.raw["record_update"]["fields_template"])


def load_policy(path: str | Path | None = None) -> CustomerOnboardingPolicy:
    policy_path = Path(path) if path else Path(os.environ.get(_POLICY_PATH_ENV_VAR) or _DEFAULT_POLICY_PATH)
    with policy_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return CustomerOnboardingPolicy(raw=raw)
