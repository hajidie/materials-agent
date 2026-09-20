"""Bounded public projections of resource operations."""
from copy import deepcopy

KINDS = ("dataset", "training_run", "model", "prediction")


def upload_view(value):
    # The original server operation contains binding fingerprints and a remote key.
    return {key: deepcopy(value[key]) for key in ("operation_id", "conversation_id", "status", "created_at",
        "lookup_status", "last_check", "remote_status", "resource_id", "reference_id", "client_idempotency_key") if key in value}
