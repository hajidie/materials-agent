from __future__ import annotations

from materialsagent.domain.ports.explanation import (
    ExplanationInput,
    ExplanationOutcome,
    ExplanationProtocolError,
    ExplanationProviderUnavailableError,
    ExplanationTimeoutError,
)


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
