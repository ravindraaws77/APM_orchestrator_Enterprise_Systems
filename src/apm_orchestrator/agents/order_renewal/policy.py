"""Loads the Order-Renewal agent's policy from YAML (never hardcoded).

Keeping "what counts as a renewal", SLA windows, and blocking-ticket
rules as data means a policy change is a config edit, not a code
change -- and the next business agent (Churn Prevention, say) is a new
policy file + a different toolbelt subset, not new plumbing (see
docs/roadmap.md in apm_connectors).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from apm_orchestrator.rag.answer import DEFAULT_MIN_SCORE, DEFAULT_TOP_K
from apm_orchestrator.rag.chunking import DEFAULT_CHUNK_CHARS, DEFAULT_OVERLAP_CHARS
from apm_orchestrator.rag.contract_terms import DEFAULT_TERM_QUESTIONS, TermQuestion
from apm_orchestrator.rag.drive_qa import DEFAULT_MAX_FILES

_DEFAULT_POLICY_PATH = Path(__file__).parent / "policy.yaml"


@dataclass(frozen=True)
class ContractTermsPolicy:
    """The ``contract_terms`` section. Every field has a default, so a
    policy file written before this section existed still loads (with
    extraction on, using rag's default questions)."""

    enabled: bool = True
    max_files: int = DEFAULT_MAX_FILES
    top_k: int = DEFAULT_TOP_K
    min_score: float = DEFAULT_MIN_SCORE
    chunk_chars: int = DEFAULT_CHUNK_CHARS
    overlap_chars: int = DEFAULT_OVERLAP_CHARS
    questions: tuple[TermQuestion, ...] = DEFAULT_TERM_QUESTIONS

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> ContractTermsPolicy:
        raw = raw or {}
        questions = raw.get("questions")
        parsed = (
            tuple(
                TermQuestion(key=q["key"], label=q.get("label", q["key"]), question=q["question"], query=q.get("query"))
                for q in questions
            )
            if questions
            else DEFAULT_TERM_QUESTIONS
        )
        if len({q.key for q in parsed}) != len(parsed):
            raise ValueError("contract_terms.questions: duplicate key")
        min_score = float(raw.get("min_score", DEFAULT_MIN_SCORE))
        if not 0.0 <= min_score <= 1.0:
            raise ValueError("contract_terms.min_score is a relevance in [0, 1], not a raw BM25 score")
        return cls(
            enabled=bool(raw.get("enabled", True)),
            max_files=int(raw.get("max_files", DEFAULT_MAX_FILES)),
            top_k=int(raw.get("top_k", DEFAULT_TOP_K)),
            min_score=min_score,
            chunk_chars=int(raw.get("chunk_chars", DEFAULT_CHUNK_CHARS)),
            overlap_chars=int(raw.get("overlap_chars", DEFAULT_OVERLAP_CHARS)),
            questions=parsed,
        )


@dataclass(frozen=True)
class OrderRenewalPolicy:
    raw: dict[str, Any] = field(repr=False)
    # First 12 hex chars of the loaded policy.yaml's own sha256 -- a
    # content hash rather than a human-maintained version number, since
    # a human editing the YAML and forgetting to bump a version field is
    # exactly the failure mode this exists to avoid (see CLAUDE.md's own
    # history of policy.yaml mistakes shipping unnoticed). Recorded once
    # per case (case_graph.py) so a decision can be tied back to the
    # exact policy snapshot that governed it -- see the observability
    # doc's "Item 8: Agent & policy drift", stage 2.
    policy_version: str = "unknown"

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
    def follow_up_issue_type(self) -> str:
        """Jira work type used when proposing a follow-up ticket."""
        return self.raw["follow_up_routing"]["issue_type"]

    @property
    def reporting_sheet(self) -> str:
        return self.raw["reporting"]["workbook_sheet"]

    @property
    def reporting_address(self) -> str:
        return self.raw["reporting"]["address"]

    @property
    def record_update_fields(self) -> dict[str, Any]:
        return dict(self.raw["record_update"]["fields_template"])

    @property
    def contract_terms(self) -> ContractTermsPolicy:
        return ContractTermsPolicy.from_raw(self.raw.get("contract_terms"))

    # -- PO intake (Phase 6.1) -------------------------------------------

    @property
    def po_min_field_confidence(self) -> float:
        return float(self.raw["po_intake"]["min_field_confidence"])

    @property
    def po_required_fields(self) -> list[str]:
        return list(self.raw["po_intake"]["required_fields"])

    @property
    def po_reconciliation(self) -> dict[str, Any]:
        return dict(self.raw["po_intake"]["reconciliation"])

    @property
    def po_record_fields(self) -> dict[str, str]:
        return dict(self.raw["po_intake"]["record_fields"])

    @property
    def po_mismatch_project_key(self) -> str:
        return self.raw["po_intake"]["mismatch_ticket"].get("project_key") or self.route_for(None)

    @property
    def po_mismatch_issue_type(self) -> str:
        return self.raw["po_intake"]["mismatch_ticket"]["issue_type"]


def load_policy(path: str | Path | None = None) -> OrderRenewalPolicy:
    policy_path = Path(path) if path else _DEFAULT_POLICY_PATH
    content = policy_path.read_bytes()
    raw = yaml.safe_load(content)
    policy_version = hashlib.sha256(content).hexdigest()[:12]
    return OrderRenewalPolicy(raw=raw, policy_version=policy_version)
