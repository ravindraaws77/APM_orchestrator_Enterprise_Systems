"""Run the real-Postgres + real-server mechanics tests with test
configuration only -- never your normal APM_CONNECTORS_* settings.

Covers tests/test_routing_log_mechanics.py (real Postgres) and
tests/test_case_graph_mechanics.py (real Postgres + a real apm_connectors
server). Without this script those tests read whatever is in your shell
and .env -- including your normal APM_CONNECTORS_BASE_URL/API key -- so
they can end up pointed at the wrong server. This script always points
them at apm_connectors' test server (its scripts/start_test_server.py)
and a disposable test database.

Setup (one-time):
  1. Create a disposable Postgres database for these tests. Its tables
     get test rows written into them -- never point it at a database
     holding real data.
  2. Put its URL in this repo's .env (or set it in the shell):
       APM_TEST_DATABASE_URL=postgresql://user:password@localhost:5432/apm_orchestrator_test

Usage:
  1. In apm_connectors:     python scripts/start_test_server.py
  2. In this repo:          python scripts/run_live_tests.py
     Extra pytest arguments pass through, e.g. -k routing, -x, -q.

Optional .env entries (defaults match start_test_server.py):
  APM_TEST_CONNECTORS_BASE_URL=http://127.0.0.1:8123
  APM_TEST_ORCHESTRATOR_KEY=orch-key
  APM_TEST_APPROVER_KEY=approver-key
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_FILES = ["tests/test_routing_log_mechanics.py", "tests/test_case_graph_mechanics.py"]

DEFAULT_BASE_URL = "http://127.0.0.1:8123"
DEFAULT_ORCHESTRATOR_KEY = "orch-key"
DEFAULT_APPROVER_KEY = "approver-key"


def _setting(name: str, dotenv: dict[str, str | None], default: str | None = None) -> str | None:
    """Shell beats .env beats the default."""
    return os.environ.get(name) or dotenv.get(name) or default


def build_test_env(dotenv: dict[str, str | None], base_env: dict[str, str]) -> dict[str, str]:
    database_url = _setting("APM_TEST_DATABASE_URL", dotenv)
    if not database_url:
        raise SystemExit(
            "APM_TEST_DATABASE_URL isn't set (shell or .env). Point it at a disposable "
            "Postgres database -- see this script's docstring."
        )
    real_database_url = _setting("DATABASE_URL", dotenv)
    if real_database_url and real_database_url == database_url:
        raise SystemExit(
            "APM_TEST_DATABASE_URL is the same as DATABASE_URL -- refusing to write test "
            "rows into your real database. Use a separate, disposable one."
        )

    env = dict(base_env)
    env.update(
        {
            "APM_TEST_DATABASE_URL": database_url,
            # The mechanics tests read these names directly. Set here, they
            # also win over any normal-run values in .env, since
            # apm_orchestrator.config's load_dotenv() never overwrites a
            # variable that's already set.
            "APM_CONNECTORS_BASE_URL": _setting("APM_TEST_CONNECTORS_BASE_URL", dotenv, DEFAULT_BASE_URL),
            "APM_CONNECTORS_API_KEY": _setting("APM_TEST_ORCHESTRATOR_KEY", dotenv, DEFAULT_ORCHESTRATOR_KEY),
            "APM_APPROVER_API_KEY": _setting("APM_TEST_APPROVER_KEY", dotenv, DEFAULT_APPROVER_KEY),
        }
    )
    return env


def _server_is_up(base_url: str) -> bool:
    try:
        return httpx.get(f"{base_url}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def main() -> int:
    from dotenv import dotenv_values

    dotenv = dotenv_values(REPO_ROOT / ".env")
    env = build_test_env(dotenv, dict(os.environ))
    base_url = env["APM_CONNECTORS_BASE_URL"]

    if not _server_is_up(base_url):
        print(
            f"No apm_connectors test server answering at {base_url}.\n"
            "Start it first, in the apm_connectors repo:  python scripts/start_test_server.py\n"
            "(Running anyway: the case-graph test will be skipped.)\n"
        )

    command = [sys.executable, "-m", "pytest", *TEST_FILES, "-v", *sys.argv[1:]]
    return subprocess.call(command, cwd=REPO_ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
