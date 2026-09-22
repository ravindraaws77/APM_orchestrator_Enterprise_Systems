"""Proves SupervisorRoutingLog (db.py) against a real Postgres connection
-- table creation, recording a decision, that list_low_confidence only
returns rows actually marked "low" (newest first), and the review/
calibration path: list_for_review's unreviewed+bucket filtering,
mark_reviewed, and calibration_report's per-bucket wrong-rate.

calibration_report() aggregates over the whole table with no way to
scope by marker, unlike the other queries here -- this test database
isn't truncated between runs (same convention test_case_graph_mechanics.py
uses), so its test asserts on the *delta* the recorded rows cause
rather than an exact total, which stays correct regardless of what
earlier runs left behind.

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


@pytest.mark.asyncio
async def test_record_returns_id_and_row_is_initially_unreviewed():
    log = SupervisorRoutingLog(TEST_DATABASE_URL)
    await log.setup()
    marker = uuid.uuid4().hex[:8]

    row_id = await log.record(
        delegate="order_renewal",
        confidence="low",
        rationale=f"needs review {marker}",
        request_excerpt=f"request {marker}",
    )
    assert isinstance(row_id, int)

    unreviewed = await log.list_for_review(confidence="low", limit=200)
    assert row_id in {r["id"] for r in unreviewed}


@pytest.mark.asyncio
async def test_list_for_review_filters_by_confidence_and_excludes_reviewed():
    log = SupervisorRoutingLog(TEST_DATABASE_URL)
    await log.setup()
    marker = uuid.uuid4().hex[:8]

    high_id = await log.record(
        delegate="order_renewal", confidence="high", rationale=f"clear {marker}", request_excerpt=f"req {marker}"
    )
    low_id = await log.record(
        delegate="customer_onboarding", confidence="low", rationale=f"unsure {marker}", request_excerpt=f"req {marker}"
    )

    # Both start unreviewed, in their respective buckets only.
    low_only = await log.list_for_review(confidence="low", limit=500)
    high_only = await log.list_for_review(confidence="high", limit=500)
    assert low_id in {r["id"] for r in low_only}
    assert low_id not in {r["id"] for r in high_only}
    assert high_id in {r["id"] for r in high_only}
    assert high_id not in {r["id"] for r in low_only}

    # Once reviewed, it drops out of the unreviewed listing.
    await log.mark_reviewed(low_id, correct=True)
    low_only_after = await log.list_for_review(confidence="low", limit=500)
    assert low_id not in {r["id"] for r in low_only_after}


@pytest.mark.asyncio
async def test_mark_reviewed_unknown_id_raises():
    log = SupervisorRoutingLog(TEST_DATABASE_URL)
    await log.setup()
    with pytest.raises(ValueError):
        await log.mark_reviewed(row_id=-1, correct=True)


@pytest.mark.asyncio
async def test_calibration_report_wrong_rate_per_bucket():
    log = SupervisorRoutingLog(TEST_DATABASE_URL)
    await log.setup()
    marker = uuid.uuid4().hex[:8]

    before = await log.calibration_report()

    # 2 high-confidence reviewed, 1 wrong.
    for i, correct in enumerate([True, False]):
        row_id = await log.record(
            delegate="order_renewal", confidence="high", rationale=f"h{i} {marker}", request_excerpt=marker
        )
        await log.mark_reviewed(row_id, correct=correct)

    # 3 low-confidence reviewed, 2 wrong -- should show a clearly higher
    # wrong-rate than the high bucket, the calibrated-signal case.
    for i, correct in enumerate([False, False, True]):
        row_id = await log.record(
            delegate="customer_onboarding", confidence="low", rationale=f"l{i} {marker}", request_excerpt=marker
        )
        await log.mark_reviewed(row_id, correct=correct)

    after = await log.calibration_report()

    assert after["high"]["reviewed"] - before["high"]["reviewed"] == 2
    assert after["high"]["wrong"] - before["high"]["wrong"] == 1
    assert after["low"]["reviewed"] - before["low"]["reviewed"] == 3
    assert after["low"]["wrong"] - before["low"]["wrong"] == 2

    # wrong_rate is internally consistent with the totals it's derived from.
    assert after["high"]["wrong_rate"] == after["high"]["wrong"] / after["high"]["reviewed"]
    assert after["low"]["wrong_rate"] == after["low"]["wrong"] / after["low"]["reviewed"]
