"""scripts/run_live_tests.py's config logic: the mechanics tests must
always be pointed at the test server and a test database, never at the
developer's normal APM_CONNECTORS_* settings."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_live_tests.py"
_spec = importlib.util.spec_from_file_location("run_live_tests", _SCRIPT)
rlt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rlt)

_VARS = (
    "APM_TEST_DATABASE_URL",
    "DATABASE_URL",
    "APM_TEST_CONNECTORS_BASE_URL",
    "APM_TEST_ORCHESTRATOR_KEY",
    "APM_TEST_APPROVER_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)


def test_requires_a_test_database():
    with pytest.raises(SystemExit):
        rlt.build_test_env({}, {})


def test_refuses_the_real_database():
    url = "postgresql://localhost/real"
    with pytest.raises(SystemExit):
        rlt.build_test_env({"APM_TEST_DATABASE_URL": url, "DATABASE_URL": url}, {})


def test_overrides_normal_connectors_settings():
    base_env = {"APM_CONNECTORS_BASE_URL": "http://127.0.0.1:8000", "APM_CONNECTORS_API_KEY": "real-key"}
    env = rlt.build_test_env({"APM_TEST_DATABASE_URL": "postgresql://localhost/t"}, base_env)
    assert env["APM_CONNECTORS_BASE_URL"] == "http://127.0.0.1:8123"
    assert env["APM_CONNECTORS_API_KEY"] == "orch-key"
    assert env["APM_APPROVER_API_KEY"] == "approver-key"
    assert env["APM_TEST_DATABASE_URL"] == "postgresql://localhost/t"


def test_custom_values_from_dotenv():
    dotenv = {
        "APM_TEST_DATABASE_URL": "postgresql://localhost/t",
        "APM_TEST_CONNECTORS_BASE_URL": "http://127.0.0.1:9000",
        "APM_TEST_ORCHESTRATOR_KEY": "o",
        "APM_TEST_APPROVER_KEY": "a",
    }
    env = rlt.build_test_env(dotenv, {})
    assert (env["APM_CONNECTORS_BASE_URL"], env["APM_CONNECTORS_API_KEY"], env["APM_APPROVER_API_KEY"]) == (
        "http://127.0.0.1:9000",
        "o",
        "a",
    )
