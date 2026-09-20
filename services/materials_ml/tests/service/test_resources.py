from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import json

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from materials_ml import EngineError
from materials_ml_service.api import create_app
from materials_ml_service.domain import DatasetAsset, MLArtifact, TrainingRun, ModelAsset, ServiceError, now
from materials_ml_service.infrastructure.database import metadata


def uploaded(service, payload, scope="scope-a", key="upload", units=None):
    return service.upload_dataset(scope, key, payload, units or {})


def submitted(service, dataset, key="train", **overrides):
    return service.submit_training(dataset.scope_id, key, dataset.id,
        {"features": ["x", "z"], "target": "strength_MPa", **overrides})


def test_actual_migration_matches_metadata_and_role_is_restricted(service):
    with service.repo.engine.connect() as connection:
        diffs = compare_metadata(MigrationContext.configure(connection, opts={
            "include_object": lambda obj, name, kind, reflected, compare: name != "ml_alembic_version"}), metadata)
        assert diffs == []
        assert connection.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")).scalar() is False
        assert connection.execute(text("SELECT version_num FROM ml_alembic_version")).scalar() == "0003_scopes"


def test_upload_digest_defaults_raw_bytes_units_and_scope(service, csv_payload):
    first = uploaded(service, csv_payload)
    assert first.status == "AVAILABLE"
    assert uploaded(service, csv_payload, units={"x": None}).id == first.id
    assert first.data["units"] == {"x": None, "z": None, "strength_MPa": None}
    for payload, units in ((csv_payload.replace(b"\r\n", b"\n"), {}), (csv_payload, {"x": "wt%"})):
        with pytest.raises(ServiceError) as error:
            uploaded(service, payload, units=units)
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert uploaded(service, csv_payload, scope="scope-b").id != first.id
    with pytest.raises(ServiceError) as error:
        service.dataset_content("scope-b", first.id)
    assert error.value.status == 404


def test_training_digest_equivalence_and_conflicts_without_fit(service, csv_payload, monkeypatch):
    from sklearn.pipeline import Pipeline
    monkeypatch.setattr(Pipeline, "fit", lambda *a, **kw: pytest.fail("Submission fitted a model"))
    dataset = uploaded(service, csv_payload, units={"strength_MPa": "MPa"})
    run = submitted(service, dataset)
    assert run.status == "PENDING"
    assert run.data["spec"]["units"] == {"x": None, "z": None, "strength_MPa": "MPa"}
    assert run.data["warnings"] == ["UNIT_UNKNOWN"]
    assert submitted(service, dataset, algorithm="LR", test_size=.2, random_state=42,
                     units={"strength_MPa": "MPa"}).id == run.id
    for changes in ({"algorithm": "RF"}, {"random_state": 7}, {"features": ["z", "x"]}, {"test_size": .3}):
        with pytest.raises(ServiceError, match="IDEMPOTENCY_CONFLICT"):
            submitted(service, dataset, **changes)
    with pytest.raises(ServiceError, match="UNIT_CONFLICT"):
        submitted(service, dataset, key="other", units={"strength_MPa": "Pa"})
    service.cancel(dataset.scope_id, run.id)
    assert submitted(service, dataset).status == "CANCELLED"
    assert submitted(service, dataset, key="explicit-retry").id != run.id


def test_database_failure_before_intent_performs_no_object_write(service, csv_payload, monkeypatch):
    @contextmanager
    def unavailable():
        raise RuntimeError("injected unavailable database")
        yield
    monkeypatch.setattr(service.repo, "transaction", unavailable)
    monkeypatch.setattr(service.storage, "put", lambda *a: pytest.fail("Unrecorded object write"))
    with pytest.raises(RuntimeError):
        uploaded(service, csv_payload)


@pytest.mark.parametrize("failure", ["unknown_put", "database_publish"])
def test_upload_reconciles_object_write_without_duplicate_identity(service, csv_payload, monkeypatch, failure):
    put = service.storage.put
    if failure == "unknown_put":
        def uncertain(ref, payload):
            put(ref, payload)
            raise ServiceError("UPLOAD_OUTCOME_UNKNOWN", 503)
        monkeypatch.setattr(service.storage, "put", uncertain)
    else:
        monkeypatch.setattr(service, "reconcile_artifact", lambda *a: (_ for _ in ()).throw(RuntimeError("lost publication")))
    with pytest.raises((ServiceError, RuntimeError)):
        uploaded(service, csv_payload)
    monkeypatch.undo()
    pending = service.list(DatasetAsset, "scope-a")[0]
    assert pending.status == "PENDING"
    service.maintain()
    assert service.get(DatasetAsset, "scope-a", pending.id).status == "AVAILABLE"
    assert uploaded(service, csv_payload).id == pending.id


def test_delete_failure_and_late_object_are_reconciled_without_revival(service, csv_payload, monkeypatch):
    dataset = uploaded(service, csv_payload)
    artifact = service.get(MLArtifact, dataset.scope_id, dataset.artifact_id)
    service.delete_dataset(dataset.scope_id, dataset.id)
    delete = service.storage.delete
    monkeypatch.setattr(service.storage, "delete", lambda *a: (_ for _ in ()).throw(ServiceError("STORAGE_UNAVAILABLE", 503)))
    service.maintain()
    assert service.get(DatasetAsset, dataset.scope_id, dataset.id).status == "DELETING"
    monkeypatch.setattr(service.storage, "delete", delete)
    service.maintain()
    assert service.get(DatasetAsset, dataset.scope_id, dataset.id).status == "DELETED"
    service.storage.put(artifact.object_ref, csv_payload)  # A delayed write after the first deletion.
    service.maintain()
    assert not service.storage.exists(artifact.object_ref)
    assert uploaded(service, csv_payload).status == "DELETED"


def test_storage_identity_conflict_is_not_deleted(service, csv_payload):
    from io import BytesIO
    dataset = uploaded(service, csv_payload)
    artifact = service.get(MLArtifact, dataset.scope_id, dataset.artifact_id)
    ref = artifact.object_ref
    service.storage.client.put_object(ref["bucket"], ref["storage_key"], BytesIO(b"foreign"), 7,
                                      metadata={"object-id": "different-owner"})
    service.delete_dataset(dataset.scope_id, dataset.id)
    service.maintain()
    assert service.get(DatasetAsset, dataset.scope_id, dataset.id).status == "DELETING"
    assert service.storage.client.stat_object(ref["bucket"], ref["storage_key"]).size == 7


def test_claim_is_single_idempotent_pending_and_failure_does_not_start(service, csv_payload):
    dataset = uploaded(service, csv_payload)
    submitted(service, dataset)
    submitted(service, dataset, key="second")
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda i: service.claim("worker-" + str(i), "claim-" + str(i)), range(2)))
    owned = [r for r in claims if r]
    assert len(owned) == 1 and owned[0].status == "PENDING"
    run = owned[0]
    key = "claim-" + run.worker_session[-1]
    assert service.claim(run.worker_session, key).claim_id == run.claim_id
    service.stopped(run.id, run.scope_id, run.worker_session, run.claim_id, "JOB_SETUP_FAILED")
    assert service.get(TrainingRun, run.scope_id, run.id).status == "FAILED"
    assert service.claim("next", "new-claim") is not None


def test_lease_expiry_fences_progress_without_requeue(service, csv_payload):
    dataset = uploaded(service, csv_payload)
    submitted(service, dataset)
    run = service.claim("worker", "claim")
    service.start(run.id, run.scope_id, run.worker_session, run.claim_id, service.job_name(run))
    with service.repo.transaction() as tx:
        expired = tx.get(TrainingRun, run.id)
        expired.heartbeat_at = now() - timedelta(seconds=31)
        expired.touch(); tx.save(expired)
    service.maintain()
    assert service.get(TrainingRun, run.scope_id, run.id).recovery_required
    with pytest.raises(ServiceError, match="STALE_CLAIM"):
        service.heartbeat(run.id, run.scope_id, run.worker_session, run.claim_id)
    assert service.claim("next", "next") is None
    service.recover(run.id, run.scope_id, run.claim_id, "new-session")
    assert service.get(TrainingRun, run.scope_id, run.id).status == "FAILED"


def test_scope_fk_and_model_success_constraint(service, csv_payload):
    dataset = uploaded(service, csv_payload)
    run = submitted(service, dataset)
    with pytest.raises(IntegrityError):
        with service.repo.engine.begin() as c:
            c.execute(text("UPDATE ml_training_run SET scope_id='foreign' WHERE id=:id"), {"id": run.id})
    with pytest.raises(IntegrityError):
        with service.repo.engine.begin() as c:
            c.execute(text("UPDATE ml_training_run SET status='SUCCEEDED' WHERE id=:id"), {"id": run.id})
    with pytest.raises(ServiceError, match="DATASET_HAS_DEPENDENCIES"):
        service.delete_dataset(dataset.scope_id, dataset.id)


def test_api_authentication_domains_and_scope(service, csv_payload):
    settings = service.settings
    resource = {"Authorization": "Bearer " + settings.resource_token.get_secret_value()}
    worker = {"Authorization": "Bearer " + settings.worker_token.get_secret_value()}
    app = create_app(settings, service, maintenance=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/scopes/a/datasets", headers=worker).status_code == 401
        assert client.get("/internal/v1/recovery", headers=resource).status_code == 401
        assert client.get("/internal/v1/recovery").status_code == 401
        assert client.get("/internal/v1/recovery", headers=worker).status_code == 200
        response = client.post("/api/v1/scopes/a/datasets", headers={**resource, "Idempotency-Key": "key"},
                               files={"file": ("ignored.csv", csv_payload, "text/csv")})
        assert response.status_code == 200, response.text
        data = response.json()
        assert "scope_id" in data and "conversation_id" not in data
        assert client.get(f"/api/v1/scopes/b/datasets/{data['id']}", headers=resource).status_code == 404
        assert client.post("/api/v1/scopes/b/training-runs", headers={**resource, "Idempotency-Key": "cross"},
            json={"dataset_id": data["id"], "features": ["x"], "target": "strength_MPa"}).status_code == 404
        assert all(s not in response.text for s in ("storage_key", "object_ref", "claim_id"))
    with pytest.raises(ValueError):
        settings.__class__(**{**settings.model_dump(), "worker_token": settings.resource_token})


@pytest.mark.parametrize("payload", [b"x,y\n" + b"1,2\n" * 100001,
    (",".join("x" + str(i) for i in range(257)) + "\n" + ",".join("1" for _ in range(257))).encode(),
    (",".join("x" + str(i) for i in range(251)) + "\n" + (",".join("1" for _ in range(251)) + "\n") * 7969).encode(),
    b"a" * (20 * 1024**2 + 1)], ids=["rows", "columns", "cells", "bytes"])
def test_dataset_limits_reject_before_resource_creation(service, payload):
    with pytest.raises(ServiceError) as error:
        uploaded(service, payload)
    assert error.value.status == 413
    assert service.list(DatasetAsset, "scope-a") == []


def test_resource_size_limit_precedes_multipart_parsing_and_auth_precedes_body(service):
    resource = {"Authorization": "Bearer " + service.settings.resource_token.get_secret_value()}
    app = create_app(service.settings, service, maintenance=False)
    with TestClient(app) as client:
        payload = b"x" * (20 * 1024**2 + 65537)
        assert client.post("/api/v1/scopes/a/datasets", content=payload).status_code == 401
        response = client.post("/api/v1/scopes/a/datasets", content=payload,
            headers={**resource, "Content-Type": "multipart/form-data; boundary=sample"})
        assert response.status_code == 413


def test_service_iam_cannot_write_outside_owned_namespace(service):
    from io import BytesIO
    from minio.error import S3Error
    with pytest.raises(S3Error) as error:
        service.storage.client.put_object(service.settings.minio_bucket, "outside-owned-prefix", BytesIO(b"x"), 1)
    assert error.value.code == "AccessDenied"


def test_stale_version_cannot_overwrite_committed_state(service, csv_payload):
    dataset = uploaded(service, csv_payload)
    stale = service.get(DatasetAsset, dataset.scope_id, dataset.id)
    service.delete_dataset(dataset.scope_id, dataset.id)
    stale.touch()
    with pytest.raises(ServiceError, match="VERSION_CONFLICT"):
        with service.repo.transaction() as tx:
            tx.save(stale)
    assert service.get(DatasetAsset, dataset.scope_id, dataset.id).status == "DELETING"
