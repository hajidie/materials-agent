from concurrent.futures import ThreadPoolExecutor
from threading import Event
from contextlib import contextmanager
from io import BytesIO
import pytest
from fastapi.testclient import TestClient

from materials_ml_service.api import create_app
from materials_ml_service.domain import DatasetAsset, ServiceError, MLArtifact


def test_closed_scope_late_object_conflict_keeps_cleanup_pending(service, csv_payload):
    dataset = service.upload_dataset("owned", "file", csv_payload, {})
    service.close_scope("owned", "close")
    for _ in range(3):
        service.maintain()
    assert not service.lookup_scope_close("owned", "close")["cleanup_pending"]
    with service.repo.transaction() as tx:
        artifact = tx.get(MLArtifact, dataset.artifact_id)
    ref = artifact.object_ref
    # Deliberately corrupt ownership metadata in this fixture's dedicated bucket.
    service.storage.client.put_object(ref["bucket"], ref["storage_key"], BytesIO(csv_payload),
        len(csv_payload), content_type=ref["media_type"], metadata={"object-id": "foreign"})
    for _ in range(2):
        service.maintain()
        assert service.lookup_scope_close("owned", "close")["cleanup_pending"]
        with service.repo.transaction() as tx:
            assert tx.get(MLArtifact, artifact.id).data["maintenance_error"] == "STORAGE_IDENTITY_CONFLICT"
        assert service.storage.client.stat_object(ref["bucket"], ref["storage_key"]).size == len(csv_payload)
    # Remove only the synthetic conflict inserted above, then reconcile its tombstone.
    service.storage.client.remove_object(ref["bucket"], ref["storage_key"])
    for _ in range(2):
        service.maintain()
    assert not service.lookup_scope_close("owned", "close")["cleanup_pending"]


def test_identity_stability_and_scope_close_tombstone(service, csv_payload):
    d = service.upload_dataset("owned", "file", csv_payload, {})
    before = service.resource_identity("owned", "dataset", d.id)
    assert before["identity_contract_version"] == "ml-resource-identity-v1"
    assert "storage_key" not in str(before)
    service.delete_dataset("owned", d.id)
    after = service.resource_identity("owned", "dataset", d.id)
    assert before["remote_identity_digest"] == after["remote_identity_digest"]
    assert after["status"] == "DELETING"
    closed = service.close_scope("owned", "close-one")
    assert closed["status"] == "CLOSED" and closed["cleanup_accepted"]
    assert service.close_scope("owned", "close-one")["status"] == "CLOSED"
    with pytest.raises(ServiceError, match="SCOPE_CLOSED"):
        service.resource_identity("owned", "dataset", d.id)
    with pytest.raises(ServiceError, match="SCOPE_CLOSED"):
        service.upload_dataset("owned", "late", csv_payload, {})
    for _ in range(3):
        service.maintain()
    with service.repo.transaction() as tx:
        artifact = tx.get(MLArtifact, d.artifact_id)
        assert artifact.status == "DELETED"
    service.storage.put(artifact.object_ref, csv_payload)
    service.maintain()
    assert not service.storage.exists(artifact.object_ref)
    assert service.close_scope("empty", "close-empty")["status"] == "CLOSED"
    with pytest.raises(ServiceError, match="SCOPE_CLOSED"):
        service.upload_dataset("empty", "late", csv_payload, {})


def test_upload_prepare_original_digest_lookup_and_busy(service, csv_payload, monkeypatch):
    p = service.prepare_dataset_upload("owned", csv_payload, {}, " example ")
    d = service.upload_dataset("owned", "file", csv_payload, {}, "example", expected_digest=p["request_digest"])
    with pytest.raises(ServiceError, match="REQUEST_DIGEST_MISMATCH"):
        service.upload_dataset("owned", "bad", csv_payload, {}, expected_digest="0" * 64)
    run = service.submit_training("owned", "training", d.id, dict(features=["x", "z"], target="strength_MPa"))
    assert service.close_scope("owned", "try-one")["status"] == "BUSY"
    assert service.get(type(run), "owned", run.id).status == "PENDING"
    service.cancel("owned", run.id)
    assert service.close_scope("owned", "try-one")["status"] == "BUSY"
    assert service.close_scope("owned", "try-two")["status"] == "CLOSED"
    monkeypatch.setattr(service, "dataset_upload_identity", lambda *args: pytest.fail("Historical identity was recomputed"))
    receipt = service.lookup_operation("owned", "dataset.upload", "file", p["request_digest"], p["digest_version"])
    assert receipt["resource"]["id"] == d.id


def test_close_wins_while_upload_paused_before_intent(service, csv_payload, monkeypatch):
    ready, release = Event(), Event()
    original = service.dataset_upload_identity
    def paused(*args):
        result = original(*args)
        ready.set(); assert release.wait(10)
        return result
    monkeypatch.setattr(service, "dataset_upload_identity", paused)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(service.upload_dataset, "owned", "file", csv_payload, {})
        assert ready.wait(10)
        assert service.close_scope("owned", "first")["status"] == "CLOSED"
        release.set()
        with pytest.raises(ServiceError, match="SCOPE_CLOSED"):
            work.result()
    with service.repo.transaction() as tx:
        assert not tx.list(DatasetAsset, scope="owned")


def test_identity_and_scope_resource_authentication(service, csv_payload):
    d = service.upload_dataset("owned", "file", csv_payload, {})
    with TestClient(create_app(service.settings, service, maintenance=False)) as http:
        root = "/api/v1/scopes/owned"
        for token in (service.settings.worker_token, service.settings.mcp_token):
            headers = {"Authorization": "Bearer " + token.get_secret_value()}
            assert http.get(root + "/resource-identities/dataset/" + d.id, headers=headers).status_code == 401
            assert http.post(root + "/close-operations", json={"operation_id": "close"}, headers=headers).status_code == 401
        http.headers["Authorization"] = "Bearer " + service.settings.resource_token.get_secret_value()
        assert http.get(root + "/resource-identities/dataset/" + d.id + "?identity_contract_version=unsupported").status_code == 409
        assert http.get("/api/v1/scopes/other/resource-identities/dataset/" + d.id).status_code == 404


@pytest.mark.parametrize("committed", [False, True])
def test_scope_close_database_failure_or_lost_commit_receipt_recovers_original(service, csv_payload, monkeypatch, committed):
    service.upload_dataset("owned", "file", csv_payload, {})
    original = service.repo.transaction
    @contextmanager
    def faulty():
        with original() as tx:
            yield tx
            if not committed:
                raise RuntimeError("Synthetic rollback")
        if committed:
            raise RuntimeError("Synthetic acknowledgement loss")
    monkeypatch.setattr(service.repo, "transaction", faulty)
    with pytest.raises(RuntimeError):
        service.close_scope("owned", "same-operation")
    monkeypatch.setattr(service.repo, "transaction", original)
    receipt = service.lookup_scope_close("owned", "same-operation")
    assert receipt["status"] == ("CLOSED" if committed else "NOT_FOUND")
    assert service.close_scope("owned", "same-operation")["status"] == "CLOSED"
