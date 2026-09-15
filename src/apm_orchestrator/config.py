from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    connectors_base_url: str
    connectors_api_key: str | None
    anthropic_api_key: str | None
    order_renewal_policy_path: str | None


def load_settings() -> Settings:
    return Settings(
        connectors_base_url=os.environ.get(
            "APM_CONNECTORS_BASE_URL", "http://127.0.0.1:8000"
        ).rstrip("/"),
        connectors_api_key=os.environ.get("APM_CONNECTORS_API_KEY") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        order_renewal_policy_path=os.environ.get("ORDER_RENEWAL_POLICY_PATH") or None,
    )
