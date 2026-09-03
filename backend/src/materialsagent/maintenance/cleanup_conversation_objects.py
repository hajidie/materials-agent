from __future__ import annotations

import argparse
from datetime import datetime, timezone

from materialsagent.application.conversation_cleanup import (
    MAX_CLEANUP_DRAIN,
    ConversationCleanupService,
)
from materialsagent.infrastructure.config import load_settings, parse_minio_config
from materialsagent.infrastructure.db.session import (
    create_engine_from_settings,
    create_session_factory,
)
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.infrastructure.storage.minio import create_minio_storage


def _bounded_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer") from None
    if not 1 <= parsed <= MAX_CLEANUP_DRAIN:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retry pending Conversation object cleanup records."
    )
    parser.add_argument("--limit", type=_bounded_limit, default=100)
    args = parser.parse_args(argv)

    settings = load_settings()
    minio = parse_minio_config(settings)
    engine = create_engine_from_settings(settings)
    storage = create_minio_storage(settings)
    session_factory = create_session_factory(engine)
    service = ConversationCleanupService(
        lambda: SQLAlchemyUnitOfWork(session_factory),
        storage,
        environment=settings.app_env,
        bucket=minio.bucket,
        storage_namespace=(
            f"minio+{'https' if minio.secure else 'http'}://{minio.endpoint}"
        ),
        process_cutoff=datetime.now(timezone.utc),
    )
    try:
        summary = service.drain(limit=args.limit)
    finally:
        storage.close()
        engine.dispose()
    print(
        "attempted={attempted} completed={completed} pending={pending} "
        "safety_blocked={safety_blocked}".format(
            attempted=summary.attempted,
            completed=summary.completed,
            pending=summary.pending,
            safety_blocked=summary.safety_blocked,
        )
    )
    return 0 if summary.pending == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
