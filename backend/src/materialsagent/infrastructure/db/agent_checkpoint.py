"""SDK cursor storage; AgentRun remains the authority for business effects."""
from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from materialsagent.infrastructure.config import AppSettings
from materialsagent.infrastructure.db.session import build_postgres_url


def _conninfo(settings: AppSettings) -> str:
    url = build_postgres_url(settings)
    return make_conninfo(
        host=url.host,
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
    )


class AgentCheckpointStore:
    def __init__(self, settings: AppSettings):
        self._conninfo = _conninfo(settings)
        self._pool: AsyncConnectionPool | None = None
        self.saver: AsyncPostgresSaver | None = None

    async def open(self, *, setup: bool = False) -> AsyncPostgresSaver:
        pool = AsyncConnectionPool(
            self._conninfo,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            min_size=1,
            max_size=4,
            open=False,
        )
        await pool.open()
        try:
            saver = AsyncPostgresSaver(pool)
            if setup:
                await saver.setup()
            else:
                # Startup validates that the explicit local-stack setup ran.
                async with pool.connection() as connection:
                    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                        await connection.execute(f"SELECT 1 FROM {table} LIMIT 0")
        except BaseException:
            await pool.close()
            raise
        self._pool, self.saver = pool, saver
        return saver

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
        self._pool = None
        self.saver = None

    async def delete(self, agent_run_id: str) -> None:
        if self.saver is None:
            raise RuntimeError("Agent checkpoint store is not open.")
        await self.saver.adelete_thread(agent_run_id)
