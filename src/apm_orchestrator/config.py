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


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


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
    )
