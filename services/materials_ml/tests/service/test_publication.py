import pytest
from contextlib import contextmanager

from materials_ml import TrainingSpec, train_regression, save_package, read_csv, EngineError
from materials_ml_service.domain import TrainingRun, ModelAsset, MLArtifact, ServiceError, MEMBERS
from materials_ml_service.infrastructure.database import Transaction
from test_resources import uploaded, submitted


def staged(service, payload, tmp_path, *, algorithm="LR", members=tuple(MEMBERS)):
    dataset = uploaded(service, payload)
    submitted(service, dataset)
    run = service.claim("worker", "claim")
    owner = (run.id, run.scope_id, run.worker_session, run.claim_id)
    service.start(*owner, service.job_name(run))
    package = train_regression(read_csv(payload), TrainingSpec(("x", "z"), "strength_MPa", algorithm=algorithm))
    location = tmp_path / "package"
    save_package(package, location)
    for member in members:
        service.upload_member(*owner, member, (location / member).read_bytes())
    return owner, location


def test_publication_transaction_rolls_back_model_and_run_together(service, csv_payload, tmp_path, monkeypatch):
    owner, _ = staged(service, csv_payload, tmp_path)
    save = Transaction.save
    def failed_commit(tx, resource):
        save(tx, resource)
        if isinstance(resource, TrainingRun) and resource.status == "SUCCEEDED":
            raise RuntimeError("Injected failure after SQL updates")
    monkeypatch.setattr(Transaction, "save", failed_commit)
    with pytest.raises(RuntimeError):
        service.complete(*owner)
    run = service.get(TrainingRun, owner[1], owner[0])
    assert run.status == "RUNNING" and run.model_id is None
    assert service.list(ModelAsset, owner[1]) == []
    monkeypatch.setattr(Transaction, "save", save)
    result = service.complete(*owner)
    assert result.status == "SUCCEEDED"
    assert service.complete(*owner).model_id == result.model_id
    with pytest.raises(ServiceError, match="RUN_ALREADY_TERMINAL"):
        service.cancel(owner[1], owner[0])
    assert len(service.list(ModelAsset, owner[1])) == 1


def test_cancel_during_verification_fences_final_publication(service, csv_payload, tmp_path, monkeypatch):
    import materials_ml_service.application as application
    owner, _ = staged(service, csv_payload, tmp_path)
    evaluate = application.evaluate_test
    def cancel_then_evaluate(*args):
        service.cancel(owner[1], owner[0])
        return evaluate(*args)
    monkeypatch.setattr(application, "evaluate_test", cancel_then_evaluate)
    with pytest.raises(ServiceError, match="RUN_NOT_PUBLISHABLE"):
        service.complete(*owner)
    assert service.list(ModelAsset, owner[1]) == []
    service.stopped(*owner, "CANCELLED")
    service.maintain()
    assert service.get(TrainingRun, owner[1], owner[0]).status == "CANCELLED"
    assert all(a.status == "DELETED" for a in service.list(MLArtifact, owner[1]) if a.run_id)


def test_partial_upload_reconciles_then_cleans_without_model(service, csv_payload, tmp_path, monkeypatch):
    owner, location = staged(service, csv_payload, tmp_path, members=("manifest.json",))
    put = service.storage.put
    def unknown(ref, payload):
        put(ref, payload)
        raise ServiceError("UPLOAD_OUTCOME_UNKNOWN", 503)
    monkeypatch.setattr(service.storage, "put", unknown)
    with pytest.raises(ServiceError):
        service.upload_member(*owner, "pipeline.joblib", (location / "pipeline.joblib").read_bytes())
    monkeypatch.undo()
    service.maintain()
    with pytest.raises(ServiceError, match="PACKAGE_INCOMPLETE"):
        service.complete(*owner)
    assert service.list(ModelAsset, owner[1]) == []
    service.stopped(*owner, "TRAINING_FAILED")
    service.maintain()
    assert all(a.status == "DELETED" for a in service.list(MLArtifact, owner[1]) if a.run_id)


def test_valid_package_with_wrong_frozen_algorithm_is_not_published(service, csv_payload, tmp_path):
    owner, _ = staged(service, csv_payload, tmp_path, algorithm="RF")
    with pytest.raises(ServiceError, match="PACKAGE_CONTRACT_MISMATCH"):
        service.complete(*owner)
    assert service.list(ModelAsset, owner[1]) == []


def test_corrupt_package_rejected_before_deserialization(service, csv_payload, tmp_path, monkeypatch):
    import joblib
    owner, _ = staged(service, csv_payload, tmp_path, members=("manifest.json", "evaluation.json", "splits.json"))
    service.upload_member(*owner, "pipeline.joblib", b"not-the-manifest-hash")
    monkeypatch.setattr(joblib, "load", lambda *a, **kw: pytest.fail("Unverified pickle was loaded"))
    with pytest.raises(EngineError):
        service.complete(*owner)
    assert service.list(ModelAsset, owner[1]) == []


def test_storage_io_is_outside_database_transactions(service, csv_payload, tmp_path, monkeypatch):
    transaction, depth = service.repo.transaction, [0]
    @contextmanager
    def tracked():
        with transaction() as tx:
            depth[0] += 1
            try:
                yield tx
            finally:
                depth[0] -= 1
    monkeypatch.setattr(service.repo, "transaction", tracked)
    for operation in ("put", "get", "exists", "delete"):
        original = getattr(service.storage, operation)
        def checked(*args, original=original):
            assert depth[0] == 0, "External storage IO held a database transaction"
            return original(*args)
        monkeypatch.setattr(service.storage, operation, checked)
    owner, _ = staged(service, csv_payload, tmp_path)
    assert service.complete(*owner).status == "SUCCEEDED"
