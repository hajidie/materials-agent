from __future__ import annotations

from dataclasses import fields, replace

import pytest
from langchain_core.tools import BaseTool, StructuredTool, tool
from pydantic import BaseModel

from materialsagent.application.langchain_tool_adapter import (
    LangChainToolAdapter,
    LangChainToolAdapterError,
)
from materialsagent.application.tool_projections import ToolProjectionService
from materialsagent.application.tool_proposals import (
    MultipleToolCallsUnsupportedError,
    ToolProposalError,
    resolve_tool_proposal,
)
from materialsagent.application.tool_invocations import (
    ExecutorRouter,
    ManagedExecutor,
    StandardSyncExecutor,
)
from materialsagent.application.tool_registry import (
    InvalidToolRegistrationError,
    ToolRegistry,
)
from materialsagent.application.unit_conversion_tool import (
    build_unit_conversion_registered_tool,
)
from materialsagent.domain.models.tool_invocation import ProposalOrigin
from materialsagent.domain.ports.tool_registry import (
    RegisteredTool,
    RoutingCatalogSnapshot,
    ToolExecutionBinding,
    ToolExecutionProfile,
)
from materialsagent.infrastructure.llm.configuration import ConfiguredRole

from materialsagent.infrastructure.db.conversation_task import MessageRow
from materialsagent.infrastructure.db.llm_call import LLMCallRow
from materialsagent.infrastructure.db.tool_invocation import (
    InvocationResultRow,
    InvocationRunRow,
)
from pydantic import SecretStr


@tool
def increment(value: int) -> dict[str, int]:
    """Increment an integer."""

    return {"value": value + 1}


class _ThirdPartyInput(BaseModel):
    text: str


class _ThirdPartyTool(BaseTool):
    name: str = "third_party_echo"
    description: str = "Echo controlled text."
    args_schema: type[BaseModel] = _ThirdPartyInput

    def _run(self, text: str) -> dict[str, str]:
        return {"text": text}


class _BrokenSchemaTool(_ThirdPartyTool):
    name: str = "broken_schema"

    def get_input_schema(self, config=None):  # type: ignore[no-untyped-def]
        del config

        class _Broken:
            @staticmethod
            def model_json_schema():
                raise RuntimeError("uncontrolled provider detail")

        return _Broken


def test_tool_definition_is_metadata_only_and_binding_owns_runtime_objects() -> None:
    registration = build_unit_conversion_registered_tool()

    assert isinstance(registration, RegisteredTool)
    assert {field.name for field in fields(registration.definition)}.isdisjoint(
        {
            "tool",
            "executor",
            "normalizer",
            "validator",
            "codec",
            "presenter",
            "health_probe",
        }
    )
    assert "executor" not in {field.name for field in fields(ToolExecutionBinding)}
    assert registration.binding.execution_target is not None


def test_safe_projections_are_separate_allowlists() -> None:
    definition = build_unit_conversion_registered_tool().definition

    catalog = ToolProjectionService.for_catalog(
        definition,
        availability="AVAILABLE",
    ).to_dict()
    llm = ToolProjectionService.for_llm(definition).to_langchain_schema()
    invocation = ToolProjectionService.for_invocation(definition).to_dict()

    assert set(catalog) == {
        "tool_id",
        "display_name",
        "description",
        "status",
        "execution_profile",
        "confirmation_required",
        "supported_outputs",
        "limitations",
        "availability",
    }
    assert set(llm["function"]) == {"name", "description", "parameters"}
    assert set(invocation) == {
        "tool_id",
        "version",
        "display_name",
        "execution_profile",
        "confirmation_required",
        "confirmation_prompt",
    }
    for projection in (catalog, llm, invocation):
        serialized = str(projection)
        assert "schema_hash" not in serialized
        assert "required_permissions" not in serialized
        assert "execution_target" not in serialized


def test_langchain_adapter_accepts_decorator_structured_and_base_tools() -> None:
    structured = StructuredTool.from_function(
        func=lambda value: {"value": value * 2},
        name="double_value",
        description="Double an integer.",
    )
    registrations = (
        LangChainToolAdapter.adapt(increment),
        LangChainToolAdapter.adapt(structured),
        LangChainToolAdapter.adapt(_ThirdPartyTool()),
    )
    registry = ToolRegistry(registrations)

    assert {item.tool_id for item in registry.list_registered()} == {
        "increment",
        "double_value",
        "third_party_echo",
    }
    adapted = registry.resolve("increment")
    assert adapted.execution_profile is ToolExecutionProfile.STANDARD
    assert adapted.binding.validator({"value": 1}) == {"value": 1}
    assert adapted.binding.execution_target.invoke({"value": 1}) == {"value": 2}


def test_langchain_adapter_rejects_async_bad_schema_and_duplicate_id() -> None:
    async def async_only(value: int) -> dict[str, int]:
        return {"value": value}

    async_tool = StructuredTool.from_function(
        coroutine=async_only,
        name="async_only",
        description="Async-only fixture.",
    )
    with pytest.raises(LangChainToolAdapterError, match="ASYNC_TOOL_UNSUPPORTED"):
        LangChainToolAdapter.adapt(async_tool)
    with pytest.raises(LangChainToolAdapterError, match="schema is invalid"):
        LangChainToolAdapter.adapt(_BrokenSchemaTool())
    adapted = LangChainToolAdapter.adapt(increment)
    with pytest.raises(InvalidToolRegistrationError, match="Duplicate"):
        ToolRegistry((adapted, adapted))


def test_executor_router_rejects_profile_incompatible_binding() -> None:
    registration = build_unit_conversion_registered_tool()
    incompatible = replace(
        registration,
        definition=replace(
            registration.definition,
            executor_id="managed_runtime",
        ),
    )
    registry = ToolRegistry((incompatible,))
    router = ExecutorRouter((StandardSyncExecutor(), ManagedExecutor()))

    with pytest.raises(ValueError, match="profile is incompatible"):
        router.validate_registration(registry.resolve(incompatible.tool_id))








def _native_config() -> ConfiguredRole:
    return ConfiguredRole(
        role="chat_orchestration",
        provider="deepseek",
        api_key=SecretStr("test"),
        model_name="test-model",
        endpoint="https://example.invalid",
        timeout_seconds=1,
        temperature=0,
        top_p=None,
        top_k=None,
        max_tokens=128,
        reasoning_mode="disabled",
        reasoning_effort=None,
        thinking_budget=None,
        response_format="json_object",
        streaming=False,
        context_window_tokens=4096,
        prompt_limit_tokens=2048,
        history_token_budget=512,
        safety_margin_tokens=128,
        tool_calling_mode="native",
    )




def test_invocation_database_metadata_uses_strong_optional_relationships() -> None:
    run_table = InvocationRunRow.__table__
    result_table = InvocationResultRow.__table__
    constraint_names = {
        constraint.name for constraint in run_table.constraints
    }

    assert MessageRow.__table__.c.task_id.nullable is True
    assert LLMCallRow.__table__.c.task_id.nullable is True
    assert LLMCallRow.__table__.c.source_message_id.nullable is False
    assert run_table.c.task_id.nullable is True
    assert {
        "ck_invocation_profile_relationship",
        "ck_invocation_result_relationship_exclusive",
        "ck_invocation_success_result_required",
        "fk_invocation_source_message_conversation",
        "fk_invocation_managed_tool_run_task",
        "uq_invocation_result",
    } <= constraint_names
    assert {
        constraint.name for constraint in result_table.constraints
    } >= {
        "fk_invocation_result_run",
        "uq_invocation_result_run",
    }
    assert not any(
        index.name == "uq_invocation_message_proposal" and index.unique
        for index in run_table.indexes
    )
