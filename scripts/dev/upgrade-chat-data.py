"""Run while services are stopped, before the 0021 upgrade. Prints targets before an explicit --apply.

Uses the existing conversation deletion/scope-close/object cleanup lifecycle.
Never clears volumes, migration history, model packages, or unrelated scopes.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "src"))
from materialsagent.infrastructure.config import load_settings
from materialsagent.infrastructure.db.session import create_engine_from_settings, create_session_factory
from materialsagent.application.chat_upgrade import inventory
from materialsagent.application.context import ActorContext
from materialsagent.application.errors import ApplicationError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--conversation", action="append", default=[])
    parser.add_argument("--all-incompatible", action="store_true",
        help="Apply to the complete, printed incompatible set after the safety preflight.")
    args = parser.parse_args()
    settings = load_settings()
    if settings.app_env not in ("local", "test") or not settings.local_actor_id:
        raise SystemExit("Local/test actor configuration required.")
    sessions = create_session_factory(create_engine_from_settings(settings))
    targets = inventory(sessions, settings.local_actor_id)
    print(json.dumps({"count": len(targets), "targets": targets}, ensure_ascii=False), flush=True)
    if not args.apply:
        return
    selected = {item["conversation_id"]: item for item in targets}
    if args.all_incompatible and args.conversation:
        raise SystemExit("Choose exact conversation IDs or --all-incompatible, not both.")
    chosen = list(selected) if args.all_incompatible else args.conversation
    if not chosen or any(identity not in selected for identity in chosen):
        raise SystemExit("Apply requires exact conversation IDs from the preflight inventory.")
    if any(selected[identity]["blocked"] for identity in chosen):
        raise SystemExit("Stopped: BUSY, UNKNOWN or unconfirmed ownership. No cleanup performed.")
    from materialsagent.main import create_app
    app = create_app(settings=settings.model_copy(update={
        "enable_dev_materials_ml_tools": False, "enable_materials_ml_resource_context": False,
    }))  # No MCP session or lifespan: do not recover unrelated old work.
    cleanup = app.state.conversation_cleanup_service
    actor = ActorContext(actor_id=settings.local_actor_id)
    for identity in chosen:
        # Re-evaluate local work just before the authoritative deletion transaction.
        current = next((item for item in inventory(sessions, actor.actor_id) if item["conversation_id"] == identity), None)
        if current is None or current["blocked"]:
            raise SystemExit("Stopped: preflight changed.")
        try:
            result = cleanup.delete(actor, identity)
        except ApplicationError as error:
            print(json.dumps({"conversation_id": identity, "stopped": error.code}), flush=True)
            raise SystemExit(1) from None
        print(json.dumps({"conversation_id": identity, "deleted": True, "cleanup_count": len(result.cleanup_ids)}), flush=True)
        outcome = cleanup.drain_exact(result.cleanup_ids)
        if outcome.pending or outcome.safety_blocked:
            raise SystemExit("Stopped: object cleanup is pending or ownership verification failed; durable records retained.")


if __name__ == "__main__":
    main()
