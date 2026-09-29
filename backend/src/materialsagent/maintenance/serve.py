"""Local Backend entrypoint with the event loop required by async psycopg on Windows."""
from __future__ import annotations

import asyncio
import sys


def selector_loop_factory(use_subprocess: bool = False):
    """Uvicorn 0.36+ chooses Proactor on Windows unless given a loop factory."""
    return asyncio.SelectorEventLoop()


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        if "--loop" not in sys.argv:
            sys.argv.extend(("--loop", "materialsagent.maintenance.serve:selector_loop_factory"))
    from uvicorn.main import main as uvicorn_main

    uvicorn_main()


if __name__ == "__main__":
    main()
