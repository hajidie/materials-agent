from __future__ import annotations

from collections.abc import Mapping
from threading import Lock

from materialsagent.application.tool_invocations import ToolExecutorContext
from materialsagent.domain.ports.tool_execution import ToolMetadata
from materialsagent.domain.ports.tool_registry import (
    ExecutionMode,
    ExecutionPolicy,
    PresentationMode,
    RegisteredTool,
    ToolDefinition,
    ToolExecutionBinding,
    ToolExecutionPolicy,
    ToolExecutionProfile,
    ToolStatus,
)


FAKE_SIDE_EFFECT_TOOL_ID = "dev_fake_side_effect"
FAKE_SIDE_EFFECT_PERMISSION = "dev:fake-side-effect"


class FakeSideEffectSink:
    def __init__(self) -> None:
        self._lock = Lock()
        self._receipts: dict[str, dict[str, object]] = {}

    def write_once(
        self,
        idempotency_key: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        with self._lock:
            existing = self._receipts.get(idempotency_key)
            if existing is not None:
                return dict(existing)
            receipt = {
                "receipt_id": f"fake_{len(self._receipts) + 1}",
                "accepted": True,
                "payload": dict(payload),
            }
            self._receipts[idempotency_key] = receipt
            return dict(receipt)

    @property
    def write_count(self) -> int:
        with self._lock:
            return len(self._receipts)


def _validate(arguments: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(arguments, Mapping) or set(arguments) != {"message"}:
        raise ValueError("Fake side-effect arguments are invalid.")
    message = arguments["message"]
    if type(message) is not str or not message.strip() or len(message) > 256:
        raise ValueError("Fake side-effect arguments are invalid.")
    return {"message": message.strip()}


class FakeSideEffectTool:
    def __init__(self, sink: FakeSideEffectSink) -> None:
        self._sink = sink

    def invoke_with_context(
        self,
        arguments: Mapping[str, object],
        context: ToolExecutorContext,
    ) -> dict[str, object]:
        return self._sink.write_once(context.idempotency_key, arguments)


def _codec(result: object) -> Mapping[str, object]:
    if not isinstance(result, Mapping):
        raise ValueError("Fake side-effect result is invalid.")
    return dict(result)


def _present(result: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "title": "开发测试副作用已确认",
        "summary": "Fake sink 已按幂等键记录一次。",
        "data": dict(result),
    }


def build_fake_side_effect_registered_tool(
    sink: FakeSideEffectSink,
) -> RegisteredTool:
    metadata = ToolMetadata(
        tool_id=FAKE_SIDE_EFFECT_TOOL_ID,
        tool_version="1",
        schema_version="1",
        display_name="Fake Side-effect Tool",
        description="仅供 test/dev 验证确认、幂等和恢复闭环。",
        material_scope="TEST_DEV_ONLY",
        enabled=True,
        supported_outputs=("fake_receipt",),
        supported_asset_types=(),
        execution_mode="IN_PROCESS_TEST_FAKE",
        requires_gpu=False,
        input_fields=(),
        output_summary=(),
        limitations=("严禁进入 production Catalog。",),
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["message"],
        "properties": {"message": {"type": "string", "maxLength": 256}},
    }
    definition = ToolDefinition(
        tool_id=FAKE_SIDE_EFFECT_TOOL_ID,
        version="1",
        status=ToolStatus.ACTIVE,
        display_name=metadata.display_name,
        description=metadata.description,
        input_schema=schema,
        proposal_schema=schema,
        output_schema={"type": "object"},
        runtime_metadata=metadata,
        supported_outputs=metadata.supported_outputs,
        supported_asset_types=(),
        limitations=metadata.limitations,
        execution_profile=ToolExecutionProfile.SIDE_EFFECT,
        execution_mode=ExecutionMode.SYNC,
        executor_id="standard_sync",
        tool_execution_policy=ToolExecutionPolicy(
            lifecycle_policy=ExecutionPolicy.ANY_TASK,
            required_permissions=(FAKE_SIDE_EFFECT_PERMISSION,),
            confirmation_required=True,
            confirmation_ttl_seconds=900,
        ),
        presentation_mode=PresentationMode.DETERMINISTIC,
        presenter_id="dev_fake_side_effect",
        confirmation_prompt=(
            "此操作会写入仅限 test/dev 的 Fake Sink；确认后将重新校验权限。"
        ),
    )
    target = FakeSideEffectTool(sink)
    return RegisteredTool(
        definition=definition,
        binding=ToolExecutionBinding(
            execution_target=target,
            validator=_validate,
            codec=_codec,
            presenter=_present,
            confirmation_preview_builder=lambda arguments: {
                "title": "确认开发测试副作用",
                "summary": str(arguments.get("message", "")),
            },
            health_probe=lambda: "AVAILABLE",
        ),
    )
