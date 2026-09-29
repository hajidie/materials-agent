"""Initialize LangGraph checkpoint tables during local stack setup."""
from __future__ import annotations

import asyncio
import sys

from materialsagent.infrastructure.config import load_settings
from materialsagent.infrastructure.db.agent_checkpoint import AgentCheckpointStore


async def _setup() -> None:
    store = AgentCheckpointStore(load_settings())
    try:
        await store.open(setup=True)
    finally:
        await store.close()


def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_setup())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
