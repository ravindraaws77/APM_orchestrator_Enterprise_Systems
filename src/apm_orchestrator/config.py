from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Defaults, shared between the dataclass fields below (so a direct
# Settings(...) construction -- e.g. in tests that don't care about
# model/budget -- doesn't have to list them) and load_settings()'s env
# parsing (so there's one source of truth, not two copies of the same
# literal).
_DEFAULT_SUPERVISOR_MODEL = "claude-sonnet-5"
_DEFAULT_BUSINESS_AGENT_MODEL = "claude-sonnet-5"
_DEFAULT_SUPERVISOR_MAX_BUDGET_USD = 0.50
_DEFAULT_BUSINESS_AGENT_MAX_BUDGET_USD = 2.00
_DEFAULT_DOCUMENT_EXTRACTION_URL = "http://127.0.0.1:8100"
_DEFAULT_DOCUMENT_EXTRACTION_MODEL = "claude-opus-5-5"
_DEFAULT_DOCUMENT_EXTRACTION_EFFORT = "high"
_DEFAULT_DOCUMENT_EXTRACTION_MAX_TOKENS = 16000
_DEFAULT_DOCUMENT_EXTRACTION_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    connectors_base_url: str
    connectors_api_key: str | None
    anthropic_api_key: str | None
    order_renewal_policy_path: str | None
    database_url: str | None
    supervisor_model: str = _DEFAULT_SUPERVISOR_MODEL
    business_agent_model: str = _DEFAULT_BUSINESS_AGENT_MODEL
    supervisor_max_budget_usd: float = _DEFAULT_SUPERVISOR_MAX_BUDGET_USD
    business_agent_max_budget_usd: float = _DEFAULT_BUSINESS_AGENT_MAX_BUDGET_USD
    # Document-extraction A2A agent (document_extraction/). The URL/key
    # are what a *caller* (a business agent's case graph) uses to reach
    # it; the server-side keys and model settings are what the agent
    # itself runs with.
    document_extraction_url: str = _DEFAULT_DOCUMENT_EXTRACTION_URL
    document_extraction_api_key: str | None = None
    document_extraction_server_api_keys: tuple[str, ...] = ()
    document_extraction_model: str = _DEFAULT_DOCUMENT_EXTRACTION_MODEL
    document_extraction_effort: str = _DEFAULT_DOCUMENT_EXTRACTION_EFFORT
    document_extraction_max_tokens: int = _DEFAULT_DOCUMENT_EXTRACTION_MAX_TOKENS
    document_extraction_max_bytes: int = _DEFAULT_DOCUMENT_EXTRACTION_MAX_BYTES


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def load_settings() -> Settings:
    return Settings(
        connectors_base_url=os.environ.get(
            "APM_CONNECTORS_BASE_URL", "http://127.0.0.1:8000"
        ).rstrip("/"),
        connectors_api_key=os.environ.get("APM_CONNECTORS_API_KEY") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        order_renewal_policy_path=os.environ.get("ORDER_RENEWAL_POLICY_PATH") or None,
        database_url=os.environ.get("DATABASE_URL") or None,
        # Explicit and pinned, not left to inherit the local Claude Code
        # CLI's own default (which claude_agent_sdk.ClaudeAgentOptions
        # falls back to silently when model=None) -- that default isn't
        # controlled by this repo and can change across a CLI update
        # with no visible diff here. Sonnet 5 over Opus-tier for both:
        # routing is a bounded classification task (one of two delegates,
        # or decline) and the business agents are structured tool
        # orchestration, not open-ended reasoning -- neither needs
        # Opus-tier capability, and evals/routing_cases.py's eval harness
        # is the way to validate a further step down to Haiku before
        # committing to it, not something to do unilaterally.
        supervisor_model=os.environ.get("SUPERVISOR_MODEL", _DEFAULT_SUPERVISOR_MODEL),
        business_agent_model=os.environ.get("BUSINESS_AGENT_MODEL", _DEFAULT_BUSINESS_AGENT_MODEL),
        # Backstops, not accuracy tradeoffs: a per-call dollar ceiling so
        # a single runaway or misbehaving call fails fast and cheaply
        # instead of silently draining the account -- the exact failure
        # mode that emptied this account's credits after one 8-prompt
        # calibration batch (FAILURES_AND_LESSONS_LEARNED.md section 10).
        # Generous enough not to cut off a legitimate real workflow.
        supervisor_max_budget_usd=_float_env("SUPERVISOR_MAX_BUDGET_USD", _DEFAULT_SUPERVISOR_MAX_BUDGET_USD),
        business_agent_max_budget_usd=_float_env(
            "BUSINESS_AGENT_MAX_BUDGET_USD", _DEFAULT_BUSINESS_AGENT_MAX_BUDGET_USD
        ),
        document_extraction_url=os.environ.get(
            "DOCUMENT_EXTRACTION_URL", _DEFAULT_DOCUMENT_EXTRACTION_URL
        ).rstrip("/"),
        document_extraction_api_key=os.environ.get("DOCUMENT_EXTRACTION_API_KEY") or None,
        document_extraction_server_api_keys=tuple(
            key.strip()
            for key in os.environ.get("DOCUMENT_EXTRACTION_SERVER_API_KEYS", "").split(",")
            if key.strip()
        ),
        # Pinned explicitly for the same reason as the models above. This
        # call site is a direct Messages API call (not ClaudeAgentOptions),
        # so its cost backstops are max_tokens plus the document size cap
        # below rather than max_budget_usd -- one bounded request per
        # document, no agent loop to run away. Validate a model or effort
        # change against evals/run_po_extraction_eval.py first.
        document_extraction_model=os.environ.get(
            "DOCUMENT_EXTRACTION_MODEL", _DEFAULT_DOCUMENT_EXTRACTION_MODEL
        ),
        document_extraction_effort=os.environ.get(
            "DOCUMENT_EXTRACTION_EFFORT", _DEFAULT_DOCUMENT_EXTRACTION_EFFORT
        ),
        document_extraction_max_tokens=_int_env(
            "DOCUMENT_EXTRACTION_MAX_TOKENS", _DEFAULT_DOCUMENT_EXTRACTION_MAX_TOKENS
        ),
        document_extraction_max_bytes=_int_env(
            "DOCUMENT_EXTRACTION_MAX_BYTES", _DEFAULT_DOCUMENT_EXTRACTION_MAX_BYTES
        ),
    )
