"""A minimal registry of case ids this repo has started, so the poller
can enumerate what to check without reaching into LangGraph's own
Postgres checkpointer tables (an internal implementation detail of that
library, not something to depend on). One small table this repo owns
and controls instead.
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
