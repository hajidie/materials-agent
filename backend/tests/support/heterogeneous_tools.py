from __future__ import annotations

from collections.abc import Callable, Mapping

from materialsagent.domain.ports.tool_execution import (
    ToolExecutionInput,
    ToolExecutionOutput,
    ToolMetadata,
    ToolRequestContext,
)
from materialsagent.domain.ports.tool_registry import (
    ExecutionPolicy,
    NeedsInputNormalization,
    ReadyNormalization,
    ToolDefinition,
    ToolStatus,
)


ML_TRAINING_TOOL_ID = "ml_training_test"
ML_TRAINING_OUTPUTS = ("training_metrics",)
ML_TRAINING_INPUT_SCHEMA = {
    "type": "object",
    "required": ["dataset", "task_type", "split_ratio"],
    "properties": {
        "dataset": {"type": "string"},
        "task_type": {"enum": ["classification", "regression"]},
        "split_ratio": {
            "type": "number",
            "exclusiveMinimum": 0,
            "exclusiveMaximum": 1,
        },
        "target_column": {"type": ["string", "null"]},
        "shuffle": {"type": "boolean"},
    },
    "additionalProperties": False,
}
ML_TRAINING_CANDIDATE_SCHEMA = {
    "type": "object",
    "required": [
        "dataset",
        "task_type",
        "split_ratio",
        "target_column",
        "shuffle",
    ],
    "properties": {
        "dataset": {"type": ["string", "null"]},
        "task_type": {
            "anyOf": [
                {"enum": ["classification", "regression"]},
                {"type": "null"},
            ]
        },
        "split_ratio": {"type": ["number", "null"]},
        "target_column": {"type": ["string", "null"]},
        "shuffle": {"type": ["boolean", "null"]},
    },
    "additionalProperties": False,
}


def normalize_ml_training_candidate(
    candidate_input: Mapping[str, object],
    prior_normalized_input: Mapping[str, object] | None = None,
) -> ReadyNormalization | NeedsInputNormalization:
    if not isinstance(candidate_input, Mapping):
        raise ValueError("ML Training candidate input must be an object.")
    if prior_normalized_input is not None and not isinstance(
        prior_normalized_input,
        Mapping,
    ):
        raise ValueError("ML Training prior input must be an object.")

    allowed_fields = {
        "dataset",
        "task_type",
        "split_ratio",
        "target_column",
        "shuffle",
    }
    if any(type(key) is not str or key not in allowed_fields for key in candidate_input):
        raise ValueError("ML Training candidate input contains an unsupported field.")
    if prior_normalized_input is not None and set(prior_normalized_input) != allowed_fields:
        raise ValueError("ML Training prior input is invalid.")

    normalized: dict[str, object] = {
        "dataset": None,
        "task_type": None,
        "split_ratio": None,
        "target_column": None,
        "shuffle": True,
    }
    if prior_normalized_input is not None:
        normalized.update(prior_normalized_input)
    normalized.update(candidate_input)

    dataset = normalized["dataset"]
    task_type = normalized["task_type"]
    split_ratio = normalized["split_ratio"]
    target_column = normalized["target_column"]
    shuffle = normalized["shuffle"]
    missing_fields = tuple(
        field_name
        for field_name in ("dataset", "task_type", "split_ratio")
        if normalized[field_name] is None
    )
    if dataset is not None and (type(dataset) is not str or not dataset.strip()):
        raise ValueError("ML Training dataset must be a non-blank fixture ID.")
    if task_type is not None and task_type not in {"classification", "regression"}:
        raise ValueError("ML Training task type is invalid.")
    if split_ratio is not None and (
        type(split_ratio) not in (int, float) or not 0 < split_ratio < 1
    ):
        raise ValueError("ML Training split ratio is invalid.")
    if target_column is not None and (
        type(target_column) is not str or not target_column.strip()
    ):
        raise ValueError("ML Training target column is invalid.")
    if type(shuffle) is not bool:
        raise ValueError("ML Training shuffle flag is invalid.")

    if missing_fields:
        return NeedsInputNormalization(
            normalized_input=normalized,
            missing_fields=missing_fields,
            ambiguous_fields=(),
            follow_up_suggestion="Provide the missing ML Training fixture parameters.",
        )
    return ReadyNormalization(
        normalized_input=normalized,
        requested_outputs=ML_TRAINING_OUTPUTS,
    )


class FakeMLTrainingTool:
    def __init__(
        self,
        metadata: ToolMetadata,
        execution_outcome: ToolExecutionOutput | None = None,
    ) -> None:
        self.metadata = metadata
        self._execution_outcome = execution_outcome
        self.validation_calls: list[dict[str, object]] = []
        self.execution_calls: list[tuple[ToolExecutionInput, ToolRequestContext]] = []

    def validate_input(
        self,
        normalized_input: dict[str, object],
        *,
        seed: int,
    ) -> ToolExecutionInput:
        normalized = normalize_ml_training_candidate(normalized_input)
        if not isinstance(normalized, ReadyNormalization):
            raise ValueError("ML Training normalized input is incomplete.")
        if type(seed) is not int or seed < 0:
            raise ValueError("seed must be a nonnegative integer.")
        snapshot = dict(normalized.normalized_input)
        self.validation_calls.append(snapshot)
        return ToolExecutionInput(
            # This test adapter deliberately maps heterogeneous fixture fields
            # into the current generic execution envelope. It performs no ML.
            process_parameters=snapshot,  # type: ignore[arg-type]
            requested_outputs=ML_TRAINING_OUTPUTS,
            runtime_parameters={"seed": seed},
        )

    def execute(
        self,
        validated_input: ToolExecutionInput,
        request_context: ToolRequestContext,
    ) -> ToolExecutionOutput:
        self.execution_calls.append((validated_input, request_context))
        if self._execution_outcome is not None:
            return self._execution_outcome
        return ToolExecutionOutput(
            status="SUCCEEDED",
            requested_outputs=ML_TRAINING_OUTPUTS,
            completed_outputs=ML_TRAINING_OUTPUTS,
            failed_outputs=(),
            data={
                "training_metrics": {
                    "fixture_score": 0.91,
                    "fixture_only": True,
                }
            },
            images=(),
            warnings=(),
            diagnostics=({"step": "test_fixture", "status": "SUCCEEDED"},),
            actual_runtime_parameters=dict(validated_input.runtime_parameters),
            model_bundle_id="ml-training-test-fixture",
            error=None,
        )

    def health_check(self) -> str:
        return "AVAILABLE"


def build_ml_training_test_definition(
    *,
    version: str = "1",
    execution_outcome: ToolExecutionOutput | None = None,
    normalization_observer: Callable[
        [Mapping[str, object], Mapping[str, object] | None],
        None,
    ]
    | None = None,
) -> ToolDefinition:
    metadata = ToolMetadata(
        tool_id=ML_TRAINING_TOOL_ID,
        tool_version="test-fixture-1",
        schema_version="test-fixture-1",
        display_name="ML Training Test Fixture",
        description="Test-only heterogeneous routing and execution fixture.",
        material_scope="TEST_FIXTURE_ONLY",
        enabled=True,
        supported_outputs=ML_TRAINING_OUTPUTS,
        supported_asset_types=(),
        execution_mode="IN_PROCESS_TEST_FAKE",
        requires_gpu=False,
        input_fields=(
            {"name": "dataset", "kind": "opaque_fixture_id"},
            {"name": "task_type", "kind": "enum"},
            {"name": "split_ratio", "kind": "ratio"},
            {"name": "target_column", "kind": "optional_text"},
            {"name": "shuffle", "kind": "boolean"},
        ),
        output_summary=({"name": "training_metrics", "kind": "test_data"},),
        limitations=(
            "Test fixture only; does not train a model or access a dataset.",
            "Does not read, upload, or persist files.",
        ),
    )
    tool = FakeMLTrainingTool(metadata, execution_outcome)

    def normalizer(
        candidate_input: Mapping[str, object],
        prior_normalized_input: Mapping[str, object] | None = None,
    ) -> ReadyNormalization | NeedsInputNormalization:
        if normalization_observer is not None:
            normalization_observer(candidate_input, prior_normalized_input)
        return normalize_ml_training_candidate(
            candidate_input,
            prior_normalized_input,
        )

    return ToolDefinition(
        tool_id=ML_TRAINING_TOOL_ID,
        version=version,
        status=ToolStatus.ACTIVE,
        execution_policy=ExecutionPolicy.ANY_TASK,
        display_name=metadata.display_name,
        description=metadata.description,
        input_schema=ML_TRAINING_INPUT_SCHEMA,
        candidate_input_schema=ML_TRAINING_CANDIDATE_SCHEMA,
        runtime_metadata=metadata,
        tool=tool,
        supported_outputs=ML_TRAINING_OUTPUTS,
        supported_asset_types=(),
        limitations=metadata.limitations,
        normalizer=normalizer,
    )
