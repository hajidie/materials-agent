from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from materials_ml_service.api import create_app
from materials_ml_service.domain import TrainingRun, ServiceError
from materials_ml_service.schemas import TrainingRequest
from test_predictions import prediction_inputs


def test_training_algorithm_contract_uses_canonical_enum_with_semantic_hints():
    schema = TrainingRequest.model_json_schema()["properties"]["algorithm"]
    assert schema["enum"] == ["LR", "RF"]
    assert "random forest" in schema["description"]
    assert TrainingRequest.model_validate({"dataset_id": "dataset", "features": ["x"],
        "target": "strength", "algorithm": "RF"}).algorithm == "RF"
    with pytest.raises(ValidationError):
        TrainingRequest.model_validate({"dataset_id": "dataset", "features": ["x"],
            "target": "strength", "algorithm": "随机森林"})


def test_prepared_digest_matches_commit_and_historical_lookup_never_normalizes(service, csv_payload):
    dataset = service.upload_dataset("scope-a", "data", csv_payload, {})
    args = {"dataset_id": dataset.id, "features": ["x", "z"], "target": "strength_MPa"}
    identity = service.prepare_operation("scope-a", "training.submit", args)
    assert service.list(TrainingRun, "scope-a", 20) == []
    expanded = {**args, "algorithm": "LR", "test_size": .2, "random_state": 42, "units": {}}
    assert service.prepare_operation("scope-a", "training.submit", expanded) == identity
    values = {k: v for k, v in args.items() if k != "dataset_id"}
    with pytest.raises(ServiceError, match="REQUEST_DIGEST_MISMATCH"):
        service.submit_training("scope-a", "train", dataset.id, values, expected_digest="0" * 64)
    assert service.list(TrainingRun, "scope-a", 20) == []
    run = service.submit_training("scope-a", "train", dataset.id, values, expected_digest=identity["request_digest"])
    assert run.data["request_digest"] == identity["request_digest"]
    with patch.object(service, "training_identity", side_effect=AssertionError("Historical lookup normalized")), \
         patch.object(service, "dataset_content", side_effect=AssertionError("Historical lookup read data")):
        receipt = service.lookup_operation("scope-a", "training.submit", "train", identity["request_digest"], identity["digest_version"])
        assert receipt["resource"]["id"] == run.id
        assert receipt["lookup_status"] == "FOUND"
        assert service.lookup_operation("scope-a", "training.submit", "absent", identity["request_digest"], identity["digest_version"])["lookup_status"] == "NOT_FOUND"
        with pytest.raises(ServiceError, match="IDEMPOTENCY_CONFLICT"):
            service.lookup_operation("scope-a", "training.submit", "train", "f" * 64, identity["digest_version"])


def test_operation_apis_are_read_only_resource_authenticated_and_scope_bound(service, csv_payload):
    dataset = service.upload_dataset("scope-a", "data", csv_payload, {})
    body = {"operation": "training.submit", "arguments": {"dataset_id": dataset.id,
        "features": ["x", "z"], "target": "strength_MPa"}}
    with TestClient(create_app(service.settings, service, maintenance=False)) as client:
        root = "/api/v1/scopes/scope-a"
        for token in (service.settings.worker_token, service.settings.mcp_token):
            assert client.post(root + "/operation-identities/prepare", json=body,
                headers={"Authorization": "Bearer " + token.get_secret_value()}).status_code == 401
        client.headers["Authorization"] = "Bearer " + service.settings.resource_token.get_secret_value()
        prepared = client.post(root + "/operation-identities/prepare", json=body)
        assert prepared.status_code == 200
        lookup = {k: v for k, v in prepared.json().items() if k != "scope_id"} | {"idempotency_key": "never-submitted"}
        assert client.post(root + "/operation-receipts/lookup", json=lookup).json()["lookup_status"] == "NOT_FOUND"
        assert client.post(root + "/operation-receipts/lookup", json={**lookup, "arguments": body["arguments"]}).status_code == 422
        assert client.post("/api/v1/scopes/other/operation-identities/prepare", json=body).status_code == 404
        assert service.list(TrainingRun, "scope-a", 20) == []


def test_prediction_prepare_digest_and_history_do_not_recompute(service, prediction_inputs):
    from materials_ml_service.domain import Prediction
    model, dataset, _ = prediction_inputs
    args = {"model_id": model, "input_dataset_id": dataset}
    prepared = service.prepare_operation("scope-a", "prediction.submit", args)
    assert service.list(Prediction, "scope-a") == []
    with pytest.raises(ServiceError, match="REQUEST_DIGEST_MISMATCH"):
        service.predict_model("scope-a", "predict", model, dataset, expected_digest="f" * 64)
    assert service.list(Prediction, "scope-a") == []
    result = service.predict_model("scope-a", "predict", model, dataset, expected_digest=prepared["request_digest"])
    assert result.status == "SUCCEEDED"
    with patch.object(service, "prediction_identity", side_effect=AssertionError("Current identity used")):
        receipt = service.lookup_operation("scope-a", "prediction.submit", "predict", prepared["request_digest"], prepared["digest_version"])
        assert receipt["resource"]["id"] == result.id and receipt["resource"]["status"] == "SUCCEEDED"
