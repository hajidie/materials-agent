from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import time

import numpy as np
import pytest
from sqlalchemy.exc import OperationalError

from materials_ml import predict, load_package, train_regression, TrainingSpec, save_package, read_csv
from materials_ml_service.domain import Prediction, MLArtifact, ServiceError, DatasetAsset
from materials_ml_service.infrastructure.database import Transaction
from materials_ml_service.prediction_application import CallCancellation
from materials_ml_service.windows import JobProcess, RunnerError
from test_publication import staged
from test_windows import process_handle, assert_exited


def publish_model(service, payload, folder, units):
    dataset = service.upload_dataset("scope-a", "training-data", payload, units)
    run = service.submit_training("scope-a", "training", dataset.id, {"features": ["x", "z"], "target": "strength_MPa"})
    run = service.claim("worker", "claim")
    owner = (run.id, run.scope_id, run.worker_session, run.claim_id)
    service.start(*owner, service.job_name(run))
    save_package(train_regression(read_csv(payload), TrainingSpec(**run.data["spec"]["engine_spec"])), folder)
    for member in ("manifest.json", "pipeline.joblib", "evaluation.json", "splits.json"):
        service.upload_member(*owner, member, (folder / member).read_bytes())
    return service.complete(*owner).model_id


@pytest.fixture
def prediction_inputs(service, csv_payload, table, tmp_path):
    owner, folder = staged(service, csv_payload, tmp_path)
    run = service.complete(*owner)
    dataset = service.upload_dataset("scope-a", "input", table[["z", "x"]].to_csv(index=False).encode(), {})
    return run.model_id, dataset.id, folder


def test_real_prediction_roundtrip_lineage_replay_and_delete(service, prediction_inputs, table):
    model, dataset, folder = prediction_inputs
    result = service.predict_model("scope-a", "prediction", model, dataset)
    assert result.status == "SUCCEEDED", result.data
    assert result.process_stopped and result.version >= 3
    payload = json.loads(service.prediction_content("scope-a", result.id))
    np.testing.assert_allclose(payload["values"], predict(load_package(folder, trusted=True), table[["x", "z"]]).values,
                               rtol=1e-10, atol=1e-12)
    assert payload["row_positions"] == list(range(len(table)))
    assert payload["target_unit"] is None
    assert result.data["warnings"] == ["UNIT_UNVERIFIED"]
    assert service.predict_model("scope-a", "prediction", model, dataset).id == result.id
    with pytest.raises(ServiceError, match="DATASET_HAS_DEPENDENCIES"):
        service.delete_dataset("scope-a", dataset)
    other = service.upload_dataset("scope-a", "other", table[["x", "z"]].to_csv(index=False).encode(), {})
    with pytest.raises(ServiceError, match="IDEMPOTENCY_CONFLICT"):
        service.predict_model("scope-a", "prediction", model, other.id)
    with pytest.raises(ServiceError, match="RESOURCE_NOT_FOUND"):
        service.predict_model("other-scope", "prediction", model, dataset)


class GatedPrediction(JobProcess):
    def release(self):
        # A real supervised process deliberately remains blocked for deterministic cancellation/timeout tests.
        pass


@pytest.mark.parametrize("stage", ["setup", "timeout", "early-cancel", "running-cancel"])
def test_prediction_start_failure_timeout_and_cancel_no_orphans(service, prediction_inputs, stage):
    model, dataset, _ = prediction_inputs
    handles = []
    def runner(*args, **kwargs):
        if stage == "setup":
            raise RunnerError("JOB_SETUP_FAILED")
        process = GatedPrediction(*args, **kwargs)
        handles.append(process_handle(process.process.pid))
        return process
    service.prediction_runner = runner
    service.prediction_timeout = .2 if stage == "timeout" else 5
    call = CallCancellation()
    if stage == "early-cancel":
        call.event.set()
    with ThreadPoolExecutor() as pool:
        future = pool.submit(service.predict_model, "scope-a", "predict", model, dataset, call)
        if stage == "running-cancel":
            deadline = time.monotonic() + 10
            while not handles and time.monotonic() < deadline:
                time.sleep(.01)
            while service.get(Prediction, *call.owner).status != "RUNNING":
                time.sleep(.01)
            assert service.cancel_prediction(*call.owner).status == "CANCELLED"
        result = future.result(timeout=15)
    assert result.status == ("CANCELLED" if "cancel" in stage else "FAILED")
    assert result.process_stopped and result.artifact_id is None
    if stage in ("setup", "early-cancel"):
        assert result.version == 1 and handles == []
    if stage == "timeout":
        assert result.data["error_code"] == "PREDICTION_TIMEOUT"
    for handle in handles:
        assert_exited(handle)
    assert service.predict_model("scope-a", "predict", model, dataset).id == result.id


def test_same_key_replay_does_not_own_cancellation_and_busy_does_not_queue(service, prediction_inputs):
    model, dataset, _ = prediction_inputs
    service.prediction_runner = GatedPrediction
    original = CallCancellation()
    with ThreadPoolExecutor() as pool:
        future = pool.submit(service.predict_model, "scope-a", "predict", model, dataset, original)
        deadline = time.monotonic() + 10
        while original.owner is None and time.monotonic() < deadline:
            time.sleep(.01)
        replay = CallCancellation()
        result = service.predict_model("scope-a", "predict", model, dataset, replay)
        assert result.id == original.owner[1] and replay.owner is None
        with pytest.raises(ServiceError, match="PREDICTION_BUSY"):
            service.predict_model("scope-a", "different-key", model, dataset)
        assert len(service.list(Prediction, "scope-a")) == 1
        original.event.set()
        assert future.result(timeout=10).status == "CANCELLED"


@pytest.mark.parametrize("fault", ["upload-before", "upload-after", "commit-before", "commit-after"])
def test_prediction_unknown_publication_reconciles_original_identity(service, prediction_inputs, monkeypatch, fault):
    model, dataset, _ = prediction_inputs
    put, transaction = service.storage.put, service.repo.transaction
    lost = []
    pending_payload = []
    def uncertain_put(ref, payload):
        pending_payload.append((ref, payload))
        if fault == "upload-after":
            put(ref, payload)
        raise ServiceError("UPLOAD_OUTCOME_UNKNOWN", 503)
    @contextmanager
    def uncertain_transaction():
        publishing = False
        with transaction() as tx:
            save = tx.save
            def hooked(resource):
                nonlocal publishing
                save(resource)
                if isinstance(resource, Prediction) and resource.status == "SUCCEEDED" and not lost:
                    publishing = True
                    if fault == "commit-before":
                        lost.append(True)
                        raise OperationalError("injected", {}, None)
            tx.save = hooked
            yield tx
        if publishing and not lost:
            lost.append(True)
            raise OperationalError("injected", {}, None)
    if fault.startswith("upload"):
        monkeypatch.setattr(service.storage, "put", uncertain_put)
    else:
        monkeypatch.setattr(service.repo, "transaction", uncertain_transaction)
    result = service.predict_model("scope-a", "prediction", model, dataset)
    assert result.status == ("SUCCEEDED" if fault == "commit-after" else "RUNNING")
    monkeypatch.setattr(service.storage, "put", put)
    monkeypatch.setattr(service.repo, "transaction", transaction)
    if fault == "upload-before":
        put(*pending_payload[0])  # Simulate the original delayed upload completing, not a second prediction.
    service.maintain()
    current = service.get(Prediction, "scope-a", result.id)
    assert current.status == "SUCCEEDED"
    assert service.predict_model("scope-a", "prediction", model, dataset).id == current.id
    assert len(service.list(Prediction, "scope-a")) == 1
    assert len([a for a in service.list(MLArtifact, "scope-a") if a.prediction_id]) == 1


def test_cancel_unknown_upload_keeps_tombstone_against_late_object(service, prediction_inputs, monkeypatch):
    model, dataset, _ = prediction_inputs
    put = service.storage.put
    saved = []
    def uncertain(ref, payload):
        saved.append((ref, payload))
        raise ServiceError("UPLOAD_OUTCOME_UNKNOWN", 503)
    monkeypatch.setattr(service.storage, "put", uncertain)
    result = service.predict_model("scope-a", "prediction", model, dataset)
    assert result.status == "RUNNING"
    assert service.cancel_prediction("scope-a", result.id).status == "CANCELLED"
    monkeypatch.setattr(service.storage, "put", put)
    service.maintain()
    put(*saved[0])
    service.maintain()
    assert not service.storage.exists(saved[0][0])
    assert service.get(Prediction, "scope-a", result.id).artifact_id is None


@pytest.mark.parametrize("declared,accepted", [(None, False), ("MM", False), ("mm", True)])
def test_known_unit_requires_exact_explicit_match(service, csv_payload, table, tmp_path, declared, accepted):
    model = publish_model(service, csv_payload, tmp_path / "model", {"x": "mm", "strength_MPa": "MPa"})
    units = {} if declared is None else {"x": declared}
    dataset = service.upload_dataset("scope-a", "input", table[["x", "z"]].to_csv(index=False).encode(), units)
    if not accepted:
        with pytest.raises(ServiceError, match="UNIT_CONFLICT"):
            service.predict_model("scope-a", "prediction", model, dataset.id)
        assert service.list(Prediction, "scope-a") == []
    else:
        result = service.predict_model("scope-a", "prediction", model, dataset.id)
        assert result.status == "SUCCEEDED"
        assert result.data["spec"]["unit_verification"] == {"x": "MATCHED", "z": "UNVERIFIED"}
        assert json.loads(service.prediction_content("scope-a", result.id))["target_unit"] == "MPa"


@pytest.mark.parametrize("change", ["extra", "missing", "text", "rows", "bytes", "unknown-unit"])
def test_prediction_input_contracts(service, prediction_inputs, table, change):
    import pandas as pd
    model, _, _ = prediction_inputs
    inputs = table[["x", "z"]].copy()
    if change == "extra":
        inputs["extra"] = 1
    elif change == "missing":
        inputs = inputs[["x"]]
    elif change == "text":
        inputs["x"] = "not-numeric"
    elif change == "rows":
        inputs = pd.concat([inputs] * 17, ignore_index=True)
    payload = inputs.to_csv(index=False).encode()
    if change == "bytes":
        payload = b'x,z\n' + (b'"' + b' ' * 3000 + b'1",2\n') * 800
    dataset = service.upload_dataset("scope-a", "input-changed", payload, {"x": "mm"} if change == "unknown-unit" else {})
    if change == "unknown-unit":
        result = service.predict_model("scope-a", "prediction", model, dataset.id)
        assert result.status == "SUCCEEDED" and result.data["warnings"] == ["UNIT_UNVERIFIED"]
    else:
        from materials_ml import EngineError
        with pytest.raises((ServiceError, EngineError)):
            service.predict_model("scope-a", "prediction", model, dataset.id)
        assert service.list(Prediction, "scope-a") == []


def test_cancel_before_publication_wins_and_success_before_cancel_is_stable(service, prediction_inputs, monkeypatch):
    model, dataset, _ = prediction_inputs
    put = service.storage.put
    def cancel_before_publish(ref, payload):
        put(ref, payload)
        with service.repo.transaction() as tx:
            prediction = tx.list(Prediction, scope="scope-a")[0]
            prediction.cancel_requested = True; prediction.touch(); tx.save(prediction)
    monkeypatch.setattr(service.storage, "put", cancel_before_publish)
    result = service.predict_model("scope-a", "prediction", model, dataset)
    # Explicit stop confirmation completes the domain cancellation, never a model/result publication.
    result = service.cancel_prediction("scope-a", result.id)
    assert result.status == "CANCELLED" and result.artifact_id is None
    service.maintain()
    monkeypatch.setattr(service.storage, "put", put)
    success = service.predict_model("scope-a", "second", model, dataset)
    assert success.status == "SUCCEEDED"
    assert service.cancel_prediction("scope-a", success.id).status == "SUCCEEDED"


@pytest.mark.parametrize("phase", ["start", "intent"])
def test_lost_start_and_publication_intent_receipt_never_reexecutes(service, prediction_inputs, monkeypatch, phase):
    model, dataset, _ = prediction_inputs
    transaction = service.repo.transaction
    lost = []
    @contextmanager
    def uncertain():
        committed = False
        with transaction() as tx:
            save = tx.save
            def hooked(resource):
                nonlocal committed
                save(resource)
                if isinstance(resource, Prediction) and resource.status == "RUNNING" and not lost:
                    committed = resource.data['phase'] == ('COMPUTING' if phase == 'start' else 'PUBLISHING')
            tx.save = hooked
            yield tx
        if committed and not lost:
            lost.append(True)
            raise OperationalError("injected", {}, None)
    monkeypatch.setattr(service.repo, "transaction", uncertain)
    result = service.predict_model("scope-a", "prediction", model, dataset)
    assert result.status == "SUCCEEDED" and lost
    assert len(service.list(Prediction, "scope-a")) == 1


def test_existing_p2_rows_survive_incremental_upgrade(service, csv_payload, tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    from materials_ml_service import admin
    model = publish_model(service, csv_payload, tmp_path / "model", {})
    before = service.model_content("scope-a", model, "manifest.json")
    config = Config()
    config.set_main_option("script_location", str(Path(admin.__file__).with_name('migrations')).replace('%', '%%'))
    config.attributes['database_url'] = service.settings.database_url.get_secret_value()
    command.downgrade(config, '0001_ml_resources')
    admin.migrate(service.settings.database_url.get_secret_value())
    assert service.model_content("scope-a", model, "manifest.json") == before


def test_three_credential_roles_are_distinct_and_mcp_is_opt_in(service):
    from pydantic import ValidationError
    from materials_ml_service.config import Settings
    values = service.settings.model_dump()
    assert values['mcp_enabled'] is False
    for token in (values['resource_token'], values['worker_token'], None):
        with pytest.raises(ValidationError):
            Settings(**{**values, 'mcp_enabled': True, 'mcp_token': token})


def test_resource_prediction_validation_returns_controlled_error(service, prediction_inputs, table):
    from fastapi.testclient import TestClient
    from materials_ml_service.api import create_app
    model, _, _ = prediction_inputs
    dataset = service.upload_dataset('scope-a', 'missing-feature', table[['x']].to_csv(index=False).encode(), {})
    with TestClient(create_app(service.settings, service, maintenance=False)) as client:
        response = client.post('/api/v1/scopes/scope-a/predictions',
            headers={'Authorization': 'Bearer ' + service.settings.resource_token.get_secret_value(), 'Idempotency-Key': 'invalid'},
            json={'model_id': model, 'input_dataset_id': dataset.id})
        assert response.status_code == 422 and response.json() == {'error': {'code': 'PREDICTION_COLUMNS_MISMATCH'}}


def test_unknown_process_stop_blocks_next_prediction_until_recovery(service, prediction_inputs):
    model, dataset, _ = prediction_inputs
    def uncertain(*args, **kwargs):
        raise RunnerError('PROCESS_STOP_UNCONFIRMED')
    service.prediction_runner = uncertain
    with pytest.raises(ServiceError, match='PREDICTION_STOP_UNCONFIRMED'):
        service.predict_model('scope-a', 'first', model, dataset)
    original = service.list(Prediction, 'scope-a')[0]
    assert original.status == 'PENDING' and not original.process_stopped
    with pytest.raises(ServiceError, match='PREDICTION_RECOVERY_REQUIRED'):
        service.predict_model('scope-a', 'second', model, dataset)
    service.recover_predictions()
    assert service.get(Prediction, 'scope-a', original.id).status == 'FAILED'
    assert len(service.list(Prediction, 'scope-a')) == 1


def test_deadline_stops_child_while_start_confirmation_is_blocked(service, prediction_inputs, monkeypatch):
    import threading
    model, dataset, _ = prediction_inputs
    start = service._start_prediction
    release, entered = threading.Event(), threading.Event()
    handles = []
    def runner(*args, **kwargs):
        process = JobProcess(*args, **kwargs)
        handles.append(process_handle(process.process.pid))
        return process
    def blocked_start(*args):
        entered.set()
        assert release.wait(10)
        return start(*args)
    service.prediction_runner = runner
    service.prediction_timeout = .25
    monkeypatch.setattr(service, '_start_prediction', blocked_start)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(service.predict_model, 'scope-a', 'prediction', model, dataset)
        try:
            assert entered.wait(10)
            assert_exited(handles[0], timeout=2000)  # Exit must precede the blocked DB operation returning.
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result.status == 'FAILED' and result.data['error_code'] == 'PREDICTION_TIMEOUT'
    assert result.version == 1  # Never committed RUNNING after the supervised child was already stopped.
