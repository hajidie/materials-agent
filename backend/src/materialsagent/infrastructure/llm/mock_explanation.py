from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json

from materialsagent.domain.ports.explanation import (
    ExplanationInput,
    ExplanationOutcome,
    ExplanationRequestMetadata,
    ExplanationProtocolError,
    ExplanationProviderUnavailableError,
    ExplanationTimeoutError,
)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _prompt_digest(value: ExplanationInput) -> str:
    safe_value = {
        "result_id": value.result_id,
        "status": value.status,
        "requested_outputs": list(value.requested_outputs),
        "completed_outputs": list(value.completed_outputs),
        "failed_outputs": list(value.failed_outputs),
        "data": _plain_json(value.data),
        "artifacts": [
            {
                "asset_id": item.asset_id,
                "role": item.role,
                "asset_type": item.asset_type,
            }
            for item in value.artifacts
        ],
        "warnings": _plain_json(value.warnings),
        "error": _plain_json(value.error),
        "process_parameters": _plain_json(value.process_parameters),
        "tool_id": value.tool_id,
        "tool_version": value.tool_version,
        "schema_version": value.schema_version,
    }
    encoded = json.dumps(
        safe_value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class MockExplanationAdapter:
    provider = "mock"
    model_name = "mock-explanation"
    prompt_template_id = "tool-result-explanation"
    prompt_template_version = "1"

    def __init__(self, *, mode: str = "success") -> None:
        if mode not in {
            "success",
            "timeout",
            "provider_unavailable",
            "protocol_error",
            "failure",
        }:
            raise ValueError("Unsupported Mock Explanation mode.")
        self.mode = mode
        self.call_count = 0

    def request_metadata(
        self,
        value: ExplanationInput,
    ) -> ExplanationRequestMetadata:
        return ExplanationRequestMetadata(
            provider=self.provider,
            model_name=self.model_name,
            prompt_template_id=self.prompt_template_id,
            prompt_template_version=self.prompt_template_version,
            prompt_digest=_prompt_digest(value),
            generation_parameters={
                "temperature": 0,
                "max_tokens": 512,
            },
        )

    def explain(self, value: ExplanationInput) -> ExplanationOutcome:
        self.call_count += 1
        if self.mode == "timeout":
            raise ExplanationTimeoutError("Explanation provider timed out.")
        if self.mode == "provider_unavailable":
            raise ExplanationProviderUnavailableError(
                "Explanation provider is unavailable."
            )
        if self.mode == "protocol_error":
            raise ExplanationProtocolError(
                "Explanation provider returned an invalid response."
            )
        if self.mode == "failure":
            return ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id="mock-explanation-request",
                error_code="EXPLANATION_FAILED",
                safe_error_message="Explanation generation failed.",
            )

        if any(
            artifact.role == "requested_output"
            for artifact in value.artifacts
        ):
            image_statement = "已生成并保存请求的扫描电镜图像。"
        elif value.artifacts:
            image_statement = "仅保存了中间图像，未获得请求的扫描电镜结果。"
        else:
            image_statement = "未生成扫描电镜图像。"
        statements = [
            f"工具结果状态为 {value.status}。",
            image_statement,
        ]
        if "mechanical_properties" in value.completed_outputs:
            yield_strength = value.data["yield_strength"]
            elongation = value.data["elongation"]
            statements.append(
                "屈服强度为 "
                f"{yield_strength['value']} {yield_strength['unit']}，"
                "延伸率为 "
                f"{elongation['value']} {elongation['unit']}。"
            )
        elif "mechanical_properties" in value.requested_outputs:
            statements.append("未获得力学性能结果，未填充任何推测值。")

        text = "".join(statements)
        return ExplanationOutcome(
            text=text,
            usage={
                "input_tokens": len(value.requested_outputs) + len(value.data),
                "output_tokens": len(text),
            },
            provider_request_id="mock-explanation-request",
            error_code=None,
            safe_error_message=None,
        )
