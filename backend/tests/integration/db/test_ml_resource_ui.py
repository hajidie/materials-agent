import pytest

from test_ml_fence import resources, DESCRIPTOR, SERVICE
from materialsagent.application.ml_resources import MLResources
from materialsagent.application.errors import ApplicationConflictError, ResourceNotFoundError


def seeded(repo):
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        return [tx.register(SERVICE, {**DESCRIPTOR, "resource_id": f"data-{i}"}, "verified") for i in range(25)]


def test_resource_list_is_local_bounded_and_ui_state_only_exposes_fence(resources):
    repo, _ = resources
    refs = seeded(repo)
    service = MLResources(repo, None)
    first = service.list("actor_1", "conversation_1", resource_type="dataset", limit=20)
    second = service.list("actor_1", "conversation_1", resource_type="dataset", after=first["next_cursor"], limit=20)
    assert [item["dataset_ordinal"] for item in first["items"] + second["items"]] == list(range(1, 26))
    assert service.ui_state("actor_1", "conversation_1") == {
        "conversation_id": "conversation_1", "fence": {"operation_id": None, "version": 0}}
    with pytest.raises(ResourceNotFoundError):
        service.ui_state("other-actor", "conversation_1")
    with pytest.raises(ApplicationConflictError):
        service.list("actor_1", "conversation_1", resource_type="model", after=refs[0]["reference_id"])


def test_upload_lookup_returns_original_key_without_remote_credentials(resources):
    repo, _ = resources
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        tx.put("upload", "operation", {"operation_id": "operation", "status": "OUTCOME_UNKNOWN",
            "service": {"endpoint": "private"}, "idempotency_key": "remote-key", "request_digest": "original",
            "conversation_id": "conversation_1"}, status="OUTCOME_UNKNOWN", operation_key="browser-original")
    service = MLResources(repo, None)
    found = service.uploads("actor_1", "conversation_1", key="browser-original")["items"]
    assert found[0]["client_idempotency_key"] == "browser-original"
    assert found[0]["status"] == "OUTCOME_UNKNOWN"
    assert "private" not in str(found) and "remote-key" not in str(found)


def test_model_discovery_checks_anchor_without_creating_selection_state(resources):
    repo, _ = resources
    with repo.transaction("actor_1", "conversation_1", writable=True) as tx:
        anchor = tx.register(SERVICE, {**DESCRIPTOR, "resource_type": "training_run", "resource_id": "training",
            "identity": {"dataset_id": "data"}}, "invocation:train")
    class Client:
        service = SERVICE
        wrong = False
        def descriptor(self, scope, kind, identity, version=None):
            if kind == "training_run":
                return {**anchor, "status": "SUCCEEDED"}
            return {**DESCRIPTOR, "resource_type": "model", "resource_id": "model", "identity": {
                "training_run_id": "other" if self.wrong else "training", "dataset_id": "data"}}
        def resource(self, scope, kind, identity, action=None):
            return {"id": identity, "scope_id": scope, "status": "SUCCEEDED", "model_id": "model"}
    client = Client()
    service = MLResources(repo, client)
    first = service.discover_model("actor_1", "conversation_1", anchor["reference_id"])
    assert service.discover_model("actor_1", "conversation_1", anchor["reference_id"]) == first
    assert set(service.ui_state("actor_1", "conversation_1")) == {"conversation_id", "fence"}
    client.wrong = True
    with pytest.raises(ApplicationConflictError):
        service.discover_model("actor_1", "conversation_1", anchor["reference_id"])
