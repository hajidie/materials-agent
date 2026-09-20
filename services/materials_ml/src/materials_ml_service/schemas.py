"""Public service contracts shared by Resource HTTP and MCP adapters."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from .domain import DatasetAsset, TrainingRun, Prediction


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TrainingRequest(Strict):
    dataset_id: str
    features: list[str]
    target: str
    algorithm: Literal["LR", "RF"] = Field(
        default="LR",
        description="Canonical regression algorithm: LR means linear regression; RF means random forest regression (随机森林回归).",
    )
    test_size: float = 0.2
    random_state: int = 42
    units: dict[str, str | None] = Field(default_factory=dict)


class PredictionRequest(Strict):
    model_id: str
    input_dataset_id: str


class DatasetRequest(Strict):
    dataset_id: str


class RunRequest(Strict):
    training_run_id: str


class OperationPreparation(Strict):
    operation: str
    arguments: dict


class OperationLookup(Strict):
    operation: str
    idempotency_key: str = Field(min_length=1, max_length=255)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    digest_version: str


def resource_view(resource):
    result = {"id": resource.id, "scope_id": resource.scope_id, "status": resource.status,
              "version": resource.version, "created_at": resource.created_at.isoformat(),
              "updated_at": resource.updated_at.isoformat()}
    if isinstance(resource, DatasetAsset):
        result.update({k: resource.data[k] for k in
                       ("identity", "raw_sha256", "analysis", "units", "display_name", "parser_contract")})
    elif isinstance(resource, TrainingRun):
        result.update(dataset_id=resource.dataset_id, model_id=resource.model_id,
            cancel_requested=resource.cancel_requested, recovery_required=resource.recovery_required,
            error_code=resource.data.get("error_code"), units=resource.data["spec"]["units"],
            warnings=resource.data["warnings"], spec=resource.data["spec"]["engine_spec"])
    elif isinstance(resource, Prediction):
        spec = resource.data["spec"]
        result.update(model_id=resource.model_id, input_dataset_id=resource.input_dataset_id,
            cancel_requested=resource.cancel_requested, error_code=resource.data.get("error_code"),
            row_count=spec["input_identity"]["row_count"], target=spec["target"], target_unit=spec["target_unit"],
            feature_order=spec["features"], model_units=spec["model_units"], input_units=spec["input_units"],
            unit_verification=spec["unit_verification"], input_identity=spec["input_identity"],
            model_manifest_sha256=spec["model_manifest_sha256"], warnings=resource.data["warnings"],
            result_ref={"type": "prediction_result", "prediction_id": resource.id} if resource.artifact_id else None)
    else:
        result.update(training_run_id=resource.run_id, manifest=resource.data["manifest"],
            metrics=resource.data["metrics"], units=resource.data["units"], warnings=resource.data["warnings"],
            members=list(resource.data["members"]))
    return result
