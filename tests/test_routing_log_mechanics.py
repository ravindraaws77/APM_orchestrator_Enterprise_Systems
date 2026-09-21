"""Proves SupervisorRoutingLog (db.py) against a real Postgres connection
-- table creation, recording a decision, and that list_low_confidence
only returns rows actually marked "low", newest first.

Skipped automatically unless APM_TEST_DATABASE_URL is configured, same
convention test_case_graph_mechanics.py uses for its own real-Postgres
tests.
"""

from __future__ import annotations

import os
import uuid

import pytest

from apm_orchestrator.db import SupervisorRoutingLog

TEST_DATABASE_URL = os.environ.get("APM_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="APM_TEST_DATABASE_URL not set -- skipping real-Postgres routing-log tests"
)


@pytest.mark.asyncio
async def test_records_and_lists_only_low_confidence_newest_first():
    log = SupervisorRoutingLog(TEST_DATABASE_URL)
    await log.setup()

    # Unique markers so this run's rows are distinguishable from any
    # left by a previous run against the same test database.
    marker = uuid.uuid4().hex[:8]

    await log.record(
        delegate="order_renewal",
        confidence="high",
        rationale=f"clear-cut renewal {marker}",
        request_excerpt=f"clear request {marker}",
    )
    await log.record(
        delegate="customer_onboarding",
        confidence="low",
        rationale=f"could plausibly be either agent {marker}",
        request_excerpt=f"ambiguous request A {marker}",
    )
    await log.record(
        delegate="order_renewal",
        confidence="low",
        rationale=f"also plausibly either agent {marker}",
        request_excerpt=f"ambiguous request B {marker}",
    )

    rows = await log.list_low_confidence(limit=50)
    matching = [r for r in rows if marker in r["rationale"]]

    assert len(matching) == 2
    assert all(r["confidence"] == "low" for r in matching)
    # newest first
    assert matching[0]["rationale"] == f"also plausibly either agent {marker}"
    assert matching[1]["rationale"] == f"could plausibly be either agent {marker}"


@pytest.mark.asyncio
async def test_limit_is_respected():
    log = SupervisorRoutingLog(TEST_DATABASE_URL)
    await log.setup()

    marker = uuid.uuid4().hex[:8]
    for i in range(3):
        await log.record(
            delegate="order_renewal",
            confidence="low",
            rationale=f"row {i} {marker}",
            request_excerpt=f"request {i} {marker}",
        )

    rows = await log.list_low_confidence(limit=2)
    assert len(rows) <= 2
