from __future__ import annotations

from collections.abc import Mapping
import inspect

from langchain_core.tools import BaseTool, StructuredTool

from materialsagent.domain.ports.tool_execution import ToolMetadata
from materialsagent.domain.ports.tool_registry import (
    ExecutionMode,
    PresentationMode,
    RegisteredTool,
    ToolDefinition,
    ToolExecutionBinding,
    ToolExecutionPolicy,
    ToolExecutionProfile,
    ToolStatus,
)


class LangChainToolAdapterError(ValueError):
    pass


def _plain_result(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None or type(value) in (str, bool, int, float):
        return {"value": value}
    raise ValueError("Tool result is not controlled JSON.")


def _default_presenter(value: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "title": "工具执行完成",
        "summary": "工具已返回结构化结果。",
        "data": dict(value),
    }


class LangChainToolAdapter:
    @staticmethod
    def adapt(
        tool: BaseTool,
        *,
        version: str = "1",
        execution_profile: ToolExecutionProfile = ToolExecutionProfile.STANDARD,
        executor_id: str = "standard_sync",
        policy: ToolExecutionPolicy | None = None,
        presenter_id: str = "deterministic",
    ) -> RegisteredTool:
        if not isinstance(tool, BaseTool):
            raise LangChainToolAdapterError("A LangChain BaseTool is required.")
        if isinstance(tool, StructuredTool) and (
            getattr(tool, "func", None) is None
            and getattr(tool, "coroutine", None) is not None
        ):
            raise LangChainToolAdapterError("ASYNC_TOOL_UNSUPPORTED")
        run_method = getattr(tool, "_run", None)
        if run_method is not None and inspect.iscoroutinefunction(run_method):
            raise LangChainToolAdapterError("ASYNC_TOOL_UNSUPPORTED")
        try:
            input_model = tool.get_input_schema()
            input_schema = input_model.model_json_schema()
        except Exception:
            raise LangChainToolAdapterError("LangChain Tool schema is invalid.") from None

        def validate(arguments: Mapping[str, object]) -> Mapping[str, object]:
            try:
                validated = input_model.model_validate(dict(arguments))
            except Exception:
                raise ValueError("Tool arguments are invalid.") from None
            return validated.model_dump(mode="json")

        metadata = ToolMetadata(
            tool_id=tool.name,
            tool_version=version,
            schema_version="langchain-json-schema-v1",
            display_name=tool.name,
            description=tool.description or tool.name,
            material_scope="GENERAL",
            enabled=True,
            supported_outputs=("structured_result",),
            supported_asset_types=(),
            execution_mode="LANGCHAIN_SYNC",
            requires_gpu=False,
            input_fields=(),
            output_summary=(),
            limitations=("同步 LangChain Tool 适配器。",),
        )
        definition = ToolDefinition(
            tool_id=tool.name,
            version=version,
            status=ToolStatus.ACTIVE,
            display_name=metadata.display_name,
            description=metadata.description,
            input_schema=input_schema,
            proposal_schema=input_schema,
            output_schema={"type": "object"},
            runtime_metadata=metadata,
            supported_outputs=metadata.supported_outputs,
            supported_asset_types=(),
            limitations=metadata.limitations,
            execution_profile=execution_profile,
            execution_mode=ExecutionMode.SYNC,
            executor_id=executor_id,
            tool_execution_policy=policy or ToolExecutionPolicy(),
            presentation_mode=PresentationMode.DETERMINISTIC,
            presenter_id=presenter_id,
            confirmation_prompt=(
                "请确认执行此副作用工具；确认后平台将重新校验权限。"
                if execution_profile is ToolExecutionProfile.SIDE_EFFECT
                else None
            ),
        )
        return RegisteredTool(
            definition=definition,
            binding=ToolExecutionBinding(
                execution_target=tool,
                validator=validate,
                codec=_plain_result,
                presenter=_default_presenter,
                health_probe=lambda: "AVAILABLE",
            ),
        )
