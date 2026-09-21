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

import psycopg

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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
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
    CaseRegistry, for the same reason."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    async def setup(self) -> None:
        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            await conn.execute(_CREATE_ROUTING_LOG_TABLE_SQL)

    async def record(self, *, delegate: str, confidence: str, rationale: str, request_excerpt: str) -> None:
        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            await conn.execute(
                "INSERT INTO supervisor_routing_log "
                "(delegate, confidence, rationale, request_excerpt) VALUES (%s, %s, %s, %s)",
                (delegate, confidence, rationale, request_excerpt),
            )

    async def list_low_confidence(self, limit: int = 50) -> list[dict]:
        """The actual answer to "does it depend on someone noticing":
        this is what a human (or a scheduled job) checks instead --
        every routing the Supervisor itself flagged as a close call,
        newest first, regardless of whether it later turned out right."""
        async with await psycopg.AsyncConnection.connect(self._database_url) as conn:
            cursor = await conn.execute(
                "SELECT delegate, confidence, rationale, request_excerpt, created_at "
                "FROM supervisor_routing_log WHERE confidence = 'low' "
                "ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            rows = await cursor.fetchall()
        return [
            {
                "delegate": row[0],
                "confidence": row[1],
                "rationale": row[2],
                "request_excerpt": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]
