from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from materialsagent.infrastructure.db.ml_resources import lock_conversation
from test_ml_fence import resources, DESCRIPTOR, SERVICE
from materialsagent.application.ml_resources import MLResources
from materialsagent.application.ml_resource_context import ResourceContextResolver
from materialsagent.domain.models.agent import AgentRun
from materialsagent.domain.ports.tool_registry import ResourceParameterSpec, ResourceProvider, ResourceType


SPEC = ResourceParameterSpec("dataset_reference", "dataset_id", ResourceType.DATASET, ResourceProvider.ML_RESOURCE)
REGISTRATION = SimpleNamespace(resource_parameters=(SPEC,))


def context_with_refs(repo, count=25):
    descriptors = {}
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        for i in range(count):
            descriptor = {**DESCRIPTOR, "resource_id": f"dataset-{i}", "status": "AVAILABLE", "files": {},
                "description": {"name": f"dataset-{i}", "columns": ["x", "strength_MPa"]}}
            tx.register(SERVICE, descriptor, "verified")
            descriptors[descriptor["resource_id"]] = descriptor
    class Client:
        service = SERVICE
        calls = []
        def descriptor(self, scope, kind, identity, version=None):
            self.calls.append((scope, kind, identity))
            return descriptors[identity]
    service = MLResources(repo, Client())
    current = AgentRun(conversation_id="conversation_1", actor_id="actor_1", source_message_id="user-message",
        goal="请分析数据")
    registry = SimpleNamespace(resolve=lambda _: REGISTRATION)
    return ResourceContextResolver(service, registry, None), current, descriptors


def test_only_selected_resource_is_verified_and_no_unique_fallback_occurs(resources):
    repo, _ = resources
    context, current, _ = context_with_refs(repo, count=20)
    projected = context.context(current)
    assert not context.resources.client.calls
    unresolved = context.resolve(current, REGISTRATION, {"dataset_id": {"_resource_unresolved": True}}, None)
    assert unresolved[1] == {"dataset_id": "Ambiguous"} and not context.resources.client.calls
    handle = projected["mapping"]["r20"]
    normalized, issues, bindings = context.resolve(current, REGISTRATION, {"dataset_id": handle}, None)
    assert issues == {} and normalized["dataset_id"] == bindings["dataset_id"].execution_value
    assert context.resources.client.calls == [(current.conversation_id, "dataset", normalized["dataset_id"])]


def test_unavailable_selected_resource_becomes_conflict_and_dispatch_rechecks(resources):
    repo, _ = resources
    context, current, descriptors = context_with_refs(repo, count=1)
    handle = context.context(current)["mapping"]["r1"]
    normalized, issues, bindings = context.resolve(current, REGISTRATION, {"dataset_id": handle}, None)
    assert issues == {}
    descriptors[normalized["dataset_id"]]["status"] = "DELETED"
    normalized, issues, rebound = context.resolve(current, REGISTRATION, {"dataset_id": handle}, None)
    assert issues == {"dataset_id": "Conflict"} and not rebound
    record = SimpleNamespace(tool_name="materials_ml_analyze_tabular_dataset", arguments={"dataset_id": bindings["dataset_id"].execution_value},
                             resource_bindings=bindings)
    with pytest.raises(Exception):
        context.dispatch(current, record, 10)


def test_migration_preserves_ordinals_and_removes_semantic_event_table(resources, temporary_database):
    from alembic import command
    from sqlalchemy import inspect
    from test_migrations import _make_alembic_config
    repo, _ = resources
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        first = tx.register(SERVICE, DESCRIPTOR, "legacy")
        second = tx.register(SERVICE, {**DESCRIPTOR, "resource_id": "other"}, "legacy")
    config = _make_alembic_config(temporary_database)
    command.downgrade(config, "0018_ml_resources")
    command.upgrade(config, "head")
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        assert tx.ref(first["reference_id"])["dataset_ordinal"] == 1
        assert tx.ref(second["reference_id"])["dataset_ordinal"] == 2
        assert tx.register(SERVICE, {**DESCRIPTOR, "resource_id": "new"}, "new")["dataset_ordinal"] == 3
        assert "ml_resource_event" not in inspect(tx.session.bind).get_table_names()


def test_ordinals_allocate_in_conversation_transaction_and_replay_never_reuses(resources):
    repo, _ = resources
    def register(i):
        with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
            return tx.register(SERVICE, {**DESCRIPTOR, "resource_id": str(i)}, "test")
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(register, range(8)))
    assert sorted(value["dataset_ordinal"] for value in values) == list(range(1, 9))
    assert register(0) == values[0]
    assert register(9)["dataset_ordinal"] == 9


def test_registration_still_obeys_conversation_deletion_fence(resources):
    repo, _ = resources
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        lock_conversation(tx.session, "actor_1", "conversation_1").deletion_fence_operation_id = "deleting"
    with pytest.raises(Exception):
        with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
            tx.register(SERVICE, DESCRIPTOR, "blocked")
