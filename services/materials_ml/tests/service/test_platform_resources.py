"""P5 real HTTP acceptance through the separately installed Backend."""
import json
import time
import httpx
import numpy as np
import pytest
import secrets
from dotenv import dotenv_values
from minio import Minio

from materials_ml import load_package, predict, read_csv
from test_platform_mcp import platform, conversation, invoke, confirm, result, ROOT, DELAY_TRAIN
from test_end_to_end import worker_process, client


@pytest.fixture
def resources_platform(platform):
    backend, ml = platform
    backend.stop()
    backend.settings["enable_materials_ml_resources"] = True
    values = dotenv_values(ROOT / ".env")
    bucket = "p5-platform-" + secrets.token_hex(8)
    endpoint = values["MINIO_ENDPOINT"].removeprefix("http://")
    store = Minio(endpoint, access_key=values["MINIO_ACCESS_KEY"], secret_key=values["MINIO_SECRET_KEY"], secure=False)
    store.make_bucket(bucket)
    backend.settings.update(minio_endpoint="http://" + endpoint, minio_access_key=values["MINIO_ACCESS_KEY"],
        minio_secret_key=values["MINIO_SECRET_KEY"], minio_bucket=bucket, minio_secure=False)
    try:
        backend.start()
        yield backend, ml
    finally:
        backend.stop()
        for item in store.list_objects(bucket, recursive=True):
            store.remove_object(bucket, item.object_name)
        store.remove_bucket(bucket)


def upload(http, scope, payload, key):
    response = http.post(f"/api/v1/conversations/{scope}/ml/datasets", headers={"Idempotency-Key": key},
        files={"file": ("data.csv", payload, "text/csv")})
    assert response.status_code == 200, response.text
    value = response.json()["data"]
    assert value["status"] == "RESOLVED", value
    return value


def register(http, scope, kind, identity):
    response = http.post(f"/api/v1/conversations/{scope}/ml/resources", json={"resource_type": kind, "resource_id": identity})
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("algorithm", ["LR", "RF"])
def test_real_platform_resources_train_predict_download_close(resources_platform, csv_payload, table, tmp_path, algorithm):
    backend, ml = resources_platform
    with httpx.Client(base_url=backend.url, timeout=70, trust_env=False) as http:
        scope = conversation(http, "scope")
        prefix = f"/api/v1/conversations/{scope}/ml"
        data = upload(http, scope, csv_payload, "data")
        assert upload(http, scope, csv_payload, "data")["resource_id"] == data["resource_id"]
        conflict = http.post(prefix + "/datasets", headers={"Idempotency-Key": "data"},
            files={"file": ("data.csv", csv_payload.replace(b"\r\n", b"\n"), "text/csv")})
        assert conflict.status_code == 409
        assert http.get(prefix + f"/resources/{data['reference_id']}/files/dataset.csv").content == csv_payload
        run = result(confirm(http, invoke(http, scope, "train_tabular_regression", {
            "dataset_id": data["resource_id"], "features": ["x", "z"], "target": "strength_MPa", "algorithm": algorithm}, "train")))
        run_ref = register(http, scope, "training_run", run["id"])
        assert "status" not in run_ref
        busy = http.delete(f"/api/v1/conversations/{scope}")
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "CONVERSATION_BUSY", busy.text
        worker = worker_process(ml)
        try:
            assert worker.wait(timeout=60) == 0, worker.stderr.read().decode(errors="replace")
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=10)
        run = http.get(prefix + f"/resources/{run_ref['reference_id']}/remote").json()["data"]
        assert run["status"] == "SUCCEEDED"
        model_ref = register(http, scope, "model", run["model_id"])
        package = tmp_path / algorithm; package.mkdir()
        for member in ("manifest.json", "pipeline.joblib", "evaluation.json", "splits.json"):
            response = http.get(prefix + f"/resources/{model_ref['reference_id']}/files/{member}")
            assert response.status_code == 200, response.text
            (package / member).write_bytes(response.content)
        incoming = table[["z", "x"]].iloc[:10]
        dataset = upload(http, scope, incoming.to_csv(index=False).encode(), "prediction-data")
        prediction = result(confirm(http, invoke(http, scope, "predict_with_model", {
            "model_id": run["model_id"], "input_dataset_id": dataset["resource_id"]}, "predict")))
        ref = register(http, scope, "prediction", prediction["id"])
        first = http.get(prefix + "/resources?limit=2").json()["data"]
        second = http.get(prefix + "/resources", params={"limit": 100, "after": first["next_cursor"]}).json()["data"]
        assert len(first["items"]) == 2 and len(first["items"] + second["items"]) == 5
        assert not ({r["reference_id"] for r in first["items"]} & {r["reference_id"] for r in second["items"]})
        response = http.get(prefix + f"/resources/{ref['reference_id']}/files/predictions.json")
        assert response.status_code == 200, response.text
        actual = response.json()
        np.testing.assert_allclose(actual["values"], predict(load_package(package, trusted=True), incoming).values, rtol=1e-10, atol=1e-12)
        other = conversation(http, "other")
        assert http.get(f"/api/v1/conversations/{other}/ml/resources/{ref['reference_id']}/remote").status_code == 404
        assert http.post(f"/api/v1/conversations/{other}/ml/resources", json={"resource_type": "model", "resource_id": run["model_id"]}).status_code == 404
        assert http.delete(f"/api/v1/conversations/{scope}").status_code == 200
        assert http.get(prefix + f"/resources/{ref['reference_id']}/remote").status_code == 404


@pytest.mark.parametrize("fault", ["upload-receipt-loss", "upload-kill", "close-receipt-loss", "close-kill", "offline-close"])
def test_real_restart_reconciles_original_upload_or_scope_close(resources_platform, csv_payload, fault):
    backend, ml = resources_platform
    with httpx.Client(base_url=backend.url, timeout=20, trust_env=False) as http:
        scope = conversation(http, "recovery")
        prefix = f"/api/v1/conversations/{scope}/ml"
        if fault.startswith("close") or fault == "offline-close":
            dataset = upload(http, scope, csv_payload, "dataset")
        waiting = None
        if fault == "offline-close":
            waiting = invoke(http, scope, "train_tabular_regression", {"dataset_id": dataset["resource_id"],
                "features": ["x", "z"], "target": "strength_MPa"}, "waiting")
        backend.stop(); backend.fault = fault; backend.start()
        if fault.startswith("upload"):
            try:
                response = http.post(prefix + "/datasets", headers={"Idempotency-Key": "dataset"},
                    files={"file": ("data.csv", csv_payload, "text/csv")})
                assert fault == "upload-receipt-loss" and response.status_code == 202, response.text
                assert response.json()["data"]["status"] == "OUTCOME_UNKNOWN"
                assert http.delete(f"/api/v1/conversations/{scope}").status_code == 409
            except (httpx.RemoteProtocolError, httpx.ReadError):
                assert fault == "upload-kill"
        else:
            if fault == "offline-close":
                ml.stop()
            try:
                response = http.delete(f"/api/v1/conversations/{scope}")
                assert response.status_code == 503, response.text
                assert response.json()["error"]["code"] == "CONVERSATION_DELETE_PENDING"
                blocked = http.post(f"/api/v1/conversations/{scope}/messages", headers={"Idempotency-Key": "new-work"},
                    json={"mode": "NEW_RUN", "content_text": "must be fenced"})
                assert blocked.status_code == 409, blocked.text
                assert http.post(prefix + "/resources", json={"resource_type": "dataset", "resource_id": "anything"}).status_code == 409
                if waiting:
                    pending = waiting["pending_execution"]
                    rejected = http.post(f"/api/v1/agent-runs/{waiting['agent_run_id']}/invocations/{pending['invocation_run_id']}/confirm",
                        json={"waiting_version": waiting["waiting_version"], "confirmation_version": pending["confirmation_version"]})
                    assert rejected.status_code == 409, rejected.text
            except (httpx.RemoteProtocolError, httpx.ReadError):
                assert fault == "close-kill"
        backend.stop(); backend.fault = None
        if fault == "offline-close":
            # Fence survives a Backend restart even while ML remains unavailable.
            backend.start()
            assert http.post(prefix + "/resources", json={"resource_type": "dataset", "resource_id": "anything"}).status_code == 409
            backend.stop(); ml.start()
        backend.start()
        if fault.startswith("upload"):
            refs = http.get(prefix + "/resources").json()["data"]["items"]
            assert len(refs) == 1
            with client(ml) as resource:
                datasets = resource.get(f"/api/v1/scopes/{scope}/datasets").json()["items"]
                assert len(datasets) == 1 and refs[0]["resource_id"] == datasets[0]["id"]
            assert upload(http, scope, csv_payload, "dataset")["resource_id"] == datasets[0]["id"]
        else:
            assert http.get(prefix + "/resources").status_code == 404
            assert http.delete(f"/api/v1/conversations/{scope}").status_code == 200


def test_local_reference_never_caches_availability(resources_platform, csv_payload):
    backend, ml = resources_platform
    with httpx.Client(base_url=backend.url, timeout=15, trust_env=False) as http, client(ml) as resource:
        scope = conversation(http, "cache")
        data = upload(http, scope, csv_payload, "data")
        prefix = f"/api/v1/conversations/{scope}/ml/resources/{data['reference_id']}"
        assert resource.delete(f"/api/v1/scopes/{scope}/datasets/{data['resource_id']}").status_code == 202
        assert http.get(prefix).status_code == 200  # historical identity fact remains
        assert http.get(prefix + "/files/dataset.csv").status_code == 409
        ml.stop()
        assert http.get(prefix + "/remote").status_code == 503
        assert http.get(prefix + "/files/dataset.csv").status_code == 503


def test_pending_upload_explicit_same_file_resume_keeps_resource_and_object_identity(resources_platform, service, csv_payload):
    from materials_ml_service.domain import DatasetAsset, MLArtifact
    backend, ml = resources_platform
    ml.stop()
    ml.program = """import sys, uvicorn
from materials_ml_service.infrastructure.storage import MinioStorage
from materials_ml_service.domain import ServiceError
def unknown(*args):
    raise ServiceError('UPLOAD_OUTCOME_UNKNOWN', 503)
MinioStorage.put = unknown
uvicorn.run('materials_ml_service.api:create_app', factory=True, host='127.0.0.1',
    port=int(sys.argv[1]), access_log=False, log_level='critical')
"""
    ml.start()
    with httpx.Client(base_url=backend.url, timeout=20, trust_env=False) as http:
        scope = conversation(http, "pending")
        prefix = f"/api/v1/conversations/{scope}/ml"
        response = http.post(prefix + "/datasets", headers={"Idempotency-Key": "file"},
            files={"file": ("data.csv", csv_payload, "text/csv")})
        assert response.status_code == 202, response.text
        operation = response.json()["data"]
        assert operation["status"] == "REMOTE_PENDING"
        with service.repo.transaction() as tx:
            original = tx.get(DatasetAsset, operation["resource_id"])
            original_object = tx.get(MLArtifact, original.artifact_id).object_ref
        assert http.delete(f"/api/v1/conversations/{scope}").status_code == 409
        backend.stop(); ml.stop(); ml.program = None; ml.start(); backend.start()
        # Startup lookup never retransmits the missing bytes.
        value = http.get(prefix + "/uploads/" + operation["operation_id"]).json()["data"]
        assert value["status"] == "REMOTE_PENDING" and "service" not in value
        resumed = upload(http, scope, csv_payload, "file")
        assert resumed["resource_id"] == operation["resource_id"]
        with service.repo.transaction() as tx:
            assert len(tx.list(DatasetAsset, scope=scope)) == 1
            assert tx.get(MLArtifact, original.artifact_id).object_ref == original_object


@pytest.mark.parametrize("fault", ["delete-commit-before", "delete-commit-after"])
def test_delete_commit_uncertainty_queries_local_completion_before_reply(resources_platform, csv_payload, fault):
    backend, _ = resources_platform
    with httpx.Client(base_url=backend.url, timeout=20, trust_env=False) as http:
        scope = conversation(http, "commit")
        upload(http, scope, csv_payload, "data")
        backend.stop(); backend.fault = fault; backend.start()
        response = http.delete(f"/api/v1/conversations/{scope}")
        assert response.status_code == (200 if fault.endswith("after") else 503), response.text
        backend.stop(); backend.fault = None; backend.start()
        assert http.get(f"/api/v1/conversations/{scope}/ml/resources").status_code == 404
        assert http.delete(f"/api/v1/conversations/{scope}").status_code == 200


def test_unknown_requires_terminal_receipt_before_conversation_delete(resources_platform, csv_payload):
    backend, ml = resources_platform
    ml.stop(); ml.program = DELAY_TRAIN; ml.start()
    backend.stop(); backend.settings["agent_standard_timeout_seconds"] = 3; backend.start()
    with httpx.Client(base_url=backend.url, timeout=30, trust_env=False) as http, client(ml) as remote:
        scope = conversation(http, "unknown")
        data = upload(http, scope, csv_payload, "data")
        waiting = invoke(http, scope, "train_tabular_regression", {"dataset_id": data["resource_id"],
            "features": ["x", "z"], "target": "strength_MPa"}, "training")
        stopped = confirm(http, waiting)
        observation = next(o for o in stopped["observations"] if o["kind"] == "TOOL_RESULT")
        assert observation["error"]["code"] == "MCP_OUTCOME_UNKNOWN" and stopped["final_answer"] is None
        inv = f"/api/v1/agent-runs/{waiting['agent_run_id']}/invocations/{observation['invocation_run_id']}"
        assert http.delete(f"/api/v1/conversations/{scope}").status_code == 409
        receipt = http.post(inv + "/reconcile").json()["data"]["remote_receipt"]
        assert receipt["resource"]["status"] == "PENDING"
        assert http.delete(f"/api/v1/conversations/{scope}").status_code == 409
        run_id = receipt["resource"]["id"]
        ref = register(http, scope, "training_run", run_id)
        cancel = http.post(f"/api/v1/conversations/{scope}/ml/resources/{ref['reference_id']}/cancel")
        assert cancel.status_code == 200 and cancel.json()["data"]["status"] == "CANCELLED"
        # Remote cancellation alone cannot reinterpret the earlier platform receipt.
        assert http.delete(f"/api/v1/conversations/{scope}").status_code == 409
        checked = http.post(inv + "/reconcile").json()["data"]
        assert checked["status"] == "OUTCOME_UNKNOWN"
        assert checked["remote_receipt"]["resource"]["status"] == "CANCELLED"
        assert len(remote.get(f"/api/v1/scopes/{scope}/training-runs").json()["items"]) == 1
        assert http.delete(f"/api/v1/conversations/{scope}").status_code == 200


def test_historical_migration_adopts_only_verified_owned_invocation(resources_platform, csv_payload):
    import psycopg
    backend, ml = resources_platform
    with httpx.Client(base_url=backend.url, timeout=30, trust_env=False) as http, client(ml) as remote:
        scope = conversation(http, "history")
        data = remote.post(f"/api/v1/scopes/{scope}/datasets", headers={"Idempotency-Key": "data"},
            files={"file": ("data.csv", csv_payload, "text/csv")}).json()
        result(confirm(http, invoke(http, scope, "train_tabular_regression", {"dataset_id": data["id"],
            "features": ["x", "z"], "target": "strength_MPa"}, "train")))
        backend.stop(); backend.fault = "historical-migration"; backend.start(); backend.fault = None
        settings = backend.settings
        with psycopg.connect(host=settings["postgres_host"], port=settings["postgres_port"],
            user=settings["postgres_user"], password=settings["postgres_password"], dbname=settings["postgres_db"]) as db:
            found = db.execute("SELECT document FROM ml_scope_binding WHERE conversation_id=%s", (scope,)).fetchone()
            assert found is not None, "Complete verified historical evidence was not adopted"
            record = found[0]
            assert record["source"].startswith("verified-history:") and record["scope_id"] == scope
        # Migration recognition does not invent a ResourceRef or availability cache.
        assert http.get(f"/api/v1/conversations/{scope}/ml/resources").json()["data"]["items"] == []
