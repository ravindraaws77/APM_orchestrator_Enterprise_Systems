"""A minimal registry of case ids this repo has started, so the poller
can enumerate what to check without reaching into LangGraph's own
Postgres checkpointer tables (an internal implementation detail of that
library, not something to depend on). One small table this repo owns
and controls instead.

Also holds `SupervisorRoutingLog` -- a separate small table for the
Supervisor's own routing decisions (see `supervisor.py`). Deliberately
not the same table as `CaseRegistry`: a routing decision happens before
any case_id exists (today, `delegate_to_*` calls `agent.py`'s free-form
Claude Agent SDK loop directly, not `case_graph.py`'s `start_case` --
see that module's docstring), and a declined request never gets a
case_id at all. Routing observability and case-level durable state are
two different concerns with two different lifetimes; forcing them into
one table would tie the correctness of one to a change in the other.
"""

from __future__ import annotations

import asyncio
import sys

import psycopg


def _run_coro_on_selector_loop(coro):
    """Drive one coroutine to completion on a private SelectorEventLoop,
    called from a worker thread (see `_run_pg`)."""
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _run_pg(coro):
    """Run one psycopg async call safely regardless of the caller's own
    event loop.

    psycopg's async mode needs a selector-based event loop, but
    `cli.py` -- the one caller that uses `SupervisorRoutingLog` -- also
    spawns a subprocess via the Claude Agent SDK (`query()` in
    `supervisor.py`), which on Windows only works under the default
    ProactorEventLoop; SelectorEventLoop can't create subprocesses
    there. The two requirements are mutually exclusive within a single
    Windows event loop (live-verified: setting the process-wide policy
    to SelectorEventLoop fixed the psycopg crash but then broke the SDK
    subprocess with `NotImplementedError`), so rather than pick one loop
    policy for the whole process, each call here -- already a fresh,
    short-lived connection, see the class docstring -- runs to
    completion in a throwaway thread with its own selector loop instead.
    On non-Windows platforms the caller's own loop already supports
    psycopg directly, so this is a no-op passthrough.
    """
    if sys.platform != "win32":
        return await coro
    return await asyncio.get_running_loop().run_in_executor(None, _run_coro_on_selector_loop, coro)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS orchestrator_cases (
    case_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_CREATE_ROUTING_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS supervisor_routing_log (
    id BIGSERIAL PRIMARY KEY,
    delegate TEXT NOT NULL,
    confidence TEXT NOT NULL,
    rationale TEXT NOT NULL,
    request_excerpt TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_correct BOOLEAN,
    reviewed_at TIMESTAMPTZ
)
"""

# ADD COLUMN IF NOT EXISTS, not just the CREATE TABLE above: a database
# that already has this table from before reviewed_correct/reviewed_at
# existed needs these added in place -- setup() must stay safe to call
# against either a brand-new or an already-running deployment.
_ADD_REVIEW_COLUMNS_SQL = """
ALTER TABLE supervisor_routing_log
    ADD COLUMN IF NOT EXISTS reviewed_correct BOOLEAN,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ
"""


class CaseRegistry:
    """Opens a short-lived connection per call rather than pooling --
    this repo's case volume doesn't warrant a pool yet, and it's simple
    to add one later without changing this class's interface."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    async def setup(self) -> None:
        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            await conn.execute(_CREATE_TABLE_SQL)

    async def register(self, case_id: str, agent: str) -> None:
        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            await conn.execute(
                "INSERT INTO orchestrator_cases (case_id, agent) VALUES (%s, %s) "
                "ON CONFLICT (case_id) DO NOTHING",
                (case_id, agent),
            )

    async def list_case_ids(self, agent: str) -> list[str]:
        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            cursor = await conn.execute(
                "SELECT case_id FROM orchestrator_cases WHERE agent = %s ORDER BY created_at",
                (agent,),
            )
            rows = await cursor.fetchall()
        return [row[0] for row in rows]


class SupervisorRoutingLog:
    """Durable record of every Supervisor routing decision, confidence
    included -- the thing a plausible-but-wrong routing needs to be
    caught after the fact instead of depending on someone noticing (see
    CLAUDE.md). Same short-lived-connection-per-call shape as
    CaseRegistry, for the same reason.

    Confidence is only useful if it's calibrated -- if a "low" call is
    no more likely to actually be wrong than a "high" one, flagging on
    it just builds a review queue nobody ends up trusting. There's no
    way to check that without ground truth, so `reviewed_correct` exists
    to capture a human's judgment (mark_reviewed) on *both* low- and
    high-confidence rows (list_for_review), and calibration_report()
    computes whether the wrong ones actually cluster in "low" once
    enough rows have been reviewed. Capturing this from day one matters
    because a routing decision made before reviewed_correct existed can
    never have its ground truth reconstructed later -- there's no
    "correct" to recover from a plain request/delegate/confidence row
    after the fact.
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    async def setup(self) -> None:
        async def _do() -> None:
            async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
                await conn.execute(_CREATE_ROUTING_LOG_TABLE_SQL)
                await conn.execute(_ADD_REVIEW_COLUMNS_SQL)

        await _run_pg(_do())

    async def record(self, *, delegate: str, confidence: str, rationale: str, request_excerpt: str) -> int:
        async def _do() -> int:
            async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
                cursor = await conn.execute(
                    "INSERT INTO supervisor_routing_log "
                    "(delegate, confidence, rationale, request_excerpt) VALUES (%s, %s, %s, %s) "
                    "RETURNING id",
                    (delegate, confidence, rationale, request_excerpt),
                )
                row = await cursor.fetchone()
            return row[0]

        return await _run_pg(_do())

    async def list_low_confidence(self, limit: int = 50) -> list[dict]:
        """The actual answer to "does it depend on someone noticing":
        this is what a human (or a scheduled job) checks instead --
        every routing the Supervisor itself flagged as a close call,
        newest first, regardless of whether it later turned out right."""
        async def _do() -> list[tuple]:
            async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
                cursor = await conn.execute(
                    "SELECT id, delegate, confidence, rationale, request_excerpt, created_at, reviewed_correct "
                    "FROM supervisor_routing_log WHERE confidence = 'low' "
                    "ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
                return await cursor.fetchall()

        rows = await _run_pg(_do())
        return [
            {
                "id": row[0],
                "delegate": row[1],
                "confidence": row[2],
                "rationale": row[3],
                "request_excerpt": row[4],
                "created_at": row[5],
                "reviewed_correct": row[6],
            }
            for row in rows
        ]

    async def list_for_review(self, *, confidence: str | None = None, limit: int = 20) -> list[dict]:
        """Unreviewed rows to sample for calibration -- deliberately not
        low-confidence-only (unlike list_low_confidence): calibration
        needs ground truth on *both* buckets to compute a wrong-rate
        per bucket, not just the ones already flagged. Pass
        confidence="high" or "low" to sample one bucket at a time (e.g.
        alternating, so review effort doesn't skew toward whichever
        bucket happens to be reviewed first). Oldest-first, not newest:
        newest-first would let a steady stream of new routings mean the
        oldest unreviewed rows never get reached, the same starvation
        the FIFO ordering here is meant to avoid."""
        query = (
            "SELECT id, delegate, confidence, rationale, request_excerpt, created_at "
            "FROM supervisor_routing_log WHERE reviewed_correct IS NULL "
        )
        params: list[object] = []
        if confidence is not None:
            query += "AND confidence = %s "
            params.append(confidence)
        query += "ORDER BY created_at ASC LIMIT %s"
        params.append(limit)

        async def _do() -> list[tuple]:
            async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
                cursor = await conn.execute(query, params)
                return await cursor.fetchall()

        rows = await _run_pg(_do())
        return [
            {
                "id": row[0],
                "delegate": row[1],
                "confidence": row[2],
                "rationale": row[3],
                "request_excerpt": row[4],
                "created_at": row[5],
            }
            for row in rows
        ]

    async def mark_reviewed(self, row_id: int, *, correct: bool) -> None:
        async def _do() -> int:
            async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
                result = await conn.execute(
                    "UPDATE supervisor_routing_log SET reviewed_correct = %s, reviewed_at = now() WHERE id = %s",
                    (correct, row_id),
                )
                return result.rowcount

        rowcount = await _run_pg(_do())
        if rowcount == 0:
            raise ValueError(f"no supervisor_routing_log row with id={row_id}")

    async def calibration_report(self) -> dict:
        """The actual calibration check: among reviewed rows, what
        fraction of each confidence bucket turned out wrong. If "low"
        isn't meaningfully worse than "high", confidence isn't carrying
        real signal -- see the class docstring."""
        async def _do() -> list[tuple]:
            async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
                cursor = await conn.execute(
                    "SELECT confidence, reviewed_correct FROM supervisor_routing_log "
                    "WHERE reviewed_correct IS NOT NULL"
                )
                return await cursor.fetchall()

        rows = await _run_pg(_do())

        buckets: dict[str, dict[str, int]] = {
            "high": {"reviewed": 0, "wrong": 0},
            "low": {"reviewed": 0, "wrong": 0},
        }
        for confidence, correct in rows:
            bucket = buckets.setdefault(confidence, {"reviewed": 0, "wrong": 0})
            bucket["reviewed"] += 1
            if not correct:
                bucket["wrong"] += 1

        return {
            bucket_name: {
                **counts,
                "wrong_rate": (counts["wrong"] / counts["reviewed"]) if counts["reviewed"] else None,
            }
            for bucket_name, counts in buckets.items()
        }
