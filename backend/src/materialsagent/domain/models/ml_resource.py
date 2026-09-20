"""Platform reference contracts. No remote lifecycle is cached here."""
from dataclasses import dataclass
from collections.abc import Mapping

IDENTITY_VERSION = "ml-resource-identity-v1"
RESOURCE_TYPES = frozenset({"dataset", "training_run", "model", "prediction"})


@dataclass(frozen=True)
class ResourceRef:
    reference_id: str
    actor_id: str
    conversation_id: str
    service_id: str
    resource_type: str
    resource_id: str
    scope_id: str
    identity_contract_version: str
    remote_identity_digest: str
    identity: Mapping
    source: str
    created_at: str
    dataset_ordinal: int | None = None
    ordinal_source: str | None = None


def descriptor_matches(reference, descriptor):
    return all(descriptor.get(k) == reference[k] for k in (
        "scope_id", "resource_type", "resource_id", "identity_contract_version", "remote_identity_digest"))
