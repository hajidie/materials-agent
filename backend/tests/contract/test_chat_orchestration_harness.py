from __future__ import annotations

import json
from types import SimpleNamespace
import pytest
from pydantic import SecretStr

from materialsagent.application.tool_registry import ToolRegistry
from materialsagent.application.zta35g_tool import build_zta35g_tool_definition
from materialsagent.domain.ports.chat_orchestration import (
    ChatOrchestrationInput,
    ToolCandidateSet,
)
from materialsagent.domain.ports.tool_registry import (
    NeedsInputNormalization,
    ReadyNormalization,
)
from materialsagent.infrastructure.config import DeepSeekConfig


_COMPLETE_PARAMETERS = {
    "solution_temperature": {"value": 1000, "unit": "°C"},
    "solution_time": {"value": 3, "unit": "h"},
    "aging_temperature": {"value": 730, "unit": "°C"},
    "aging_time": {"value": 3, "unit": "h"},
}


def _config() -> DeepSeekConfig:
    return DeepSeekConfig(
        api_key=SecretStr("offline-placeholder-never-sent"),
        model_name="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        timeout_seconds=60.0,
    )


def _chat_input(content: str) -> ChatOrchestrationInput:
    return ChatOrchestrationInput(
        task_id="task_harness_eval",
        conversation_id="conversation_harness_eval",
        request_id="request_harness_eval",
        content_text=content,
        routing_catalog=_registry().routing_snapshot(),
    )


def _registry() -> ToolRegistry:
    return ToolRegistry((build_zta35g_tool_definition(),))


class _FixtureReplayRunnable:
    def __init__(self, user_text: str, payload: dict[str, object]) -> None:
        from materialsagent.infrastructure.llm.deepseek_chat import (
            ProviderChatResponse,
        )

        self._user_text = user_text
        self._payload = payload
        self._parsed = ProviderChatResponse.model_validate(payload)
        self.call_count = 0

    def invoke(self, messages: object) -> object:
        assert isinstance(messages, list)
        assert messages[-1] == {"role": "user", "content": self._user_text}
        self.call_count += 1
        return {
            "raw": SimpleNamespace(
                content=json.dumps(self._payload, ensure_ascii=False),
                usage_metadata=None,
                response_metadata={"headers": {}},
                id="completion-id-must-not-be-used",
            ),
            "parsed": self._parsed,
            "parsing_error": None,
        }


def test_provider_visible_messages_define_output_and_missing_material_semantics() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        _render_messages,
    )

    messages = _render_messages(_chat_input("自然语言样例输入"))

    assert [message["role"] for message in messages] == ["system", "user"]
    system = messages[0]["content"]
    for semantic_anchor in (
        "routing catalog",
        "KNOWLEDGE_ANSWER",
        "TOOL_CANDIDATES",
        "one to five unique candidates",
        "Preserve missing values as null",
        "Never return task status",
        "zta35g_sem_virtual_lab",
        "solution_temperature",
        '"additionalProperties":false',
        "schema_hash",
    ):
        assert semantic_anchor in system
    assert messages[1]["content"] == "自然语言样例输入"


@pytest.mark.parametrize(
    (
        "user_text",
        "material",
        "requested_outputs",
        "parameters",
        "missing_fields",
    ),
    [
        (
            "对ZTA35G按固溶1000°C 3h、时效730°C 3h生成一张SEM图，只要图像。",
            "ZTA35G",
            ("sem_image",),
            _COMPLETE_PARAMETERS,
            (),
        ),
        (
            "对ZTA35G用固溶1000°C 3h、时效730°C 3h预测屈服强度和延伸率，只要性能数据。",
            "ZTA35G",
            ("mechanical_properties",),
            _COMPLETE_PARAMETERS,
            (),
        ),
        (
            "对ZTA35G按固溶1000°C 3h、时效730°C 3h同时生成SEM图像，并预测屈服强度和延伸率。",
            "ZTA35G",
            ("sem_image", "mechanical_properties"),
            _COMPLETE_PARAMETERS,
            (),
        ),
        (
            "这块ZTA35G经1000°C固溶3小时、730°C时效3小时后组织长什么样？顺便告诉我有多强、还能拉伸多少。",
            "ZTA35G",
            ("sem_image", "mechanical_properties"),
            _COMPLETE_PARAMETERS,
            (),
        ),
        (
            "ZTA35G固溶1000°C 3h、时效730°C 3h，只看显微组织，别做强度和塑性预测。",
            "ZTA35G",
            ("sem_image",),
            _COMPLETE_PARAMETERS,
            (),
        ),
        (
            "ZTA35G固溶1000°C 3h、时效730°C 3h，只给屈服强度和延伸率，SEM仅作内部步骤，不要交付图像。",
            "ZTA35G",
            ("mechanical_properties",),
            _COMPLETE_PARAMETERS,
            (),
        ),
        (
            "ZTA35G固溶1000°C 3h、时效730°C后，生成显微组织图并给出屈服强度和延伸率。",
            "ZTA35G",
            ("sem_image", "mechanical_properties"),
            {
                "solution_temperature": {"value": 1000, "unit": "°C"},
                "solution_time": {"value": 3, "unit": "h"},
                "aging_temperature": {"value": 730, "unit": "°C"},
                "aging_time": None,
            },
            ("aging_time",),
        ),
        (
            "这个材料固溶1000°C 3h、时效730°C 3h后，生成显微组织图并给出屈服强度和延伸率。",
            None,
            ("sem_image", "mechanical_properties"),
            _COMPLETE_PARAMETERS,
            ("material",),
        ),
    ],
    ids=(
        "image-only",
        "properties-only",
        "image-and-properties",
        "natural-expression",
        "explicitly-exclude-properties",
        "explicitly-exclude-image",
        "missing-parameter-keeps-output-intent",
        "missing-material-keeps-output-intent",
    ),
)
def test_offline_adapter_contract_maps_fixture_payload_once(
    user_text: str,
    material: str | None,
    requested_outputs: tuple[str, ...],
    parameters: dict[str, object],
    missing_fields: tuple[str, ...],
) -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import (
        DeepSeekChatAdapter,
    )

    payload: dict[str, object] = {
        "route": "TOOL_CANDIDATES",
        "candidates": [
            {
                "tool_id": "zta35g_sem_virtual_lab",
                "candidate_input": {
                    "material": material,
                    **parameters,
                    "requested_outputs": list(requested_outputs),
                },
            },
        ],
    }
    runnable = _FixtureReplayRunnable(user_text, payload)

    outcome = DeepSeekChatAdapter(
        _config(),
        structured_runnable=runnable,
    ).orchestrate(_chat_input(user_text))

    assert isinstance(outcome.result, ToolCandidateSet)
    assert len(outcome.result.candidates) == 1
    candidate_input = outcome.result.candidates[0].candidate_input
    assert candidate_input["requested_outputs"] == requested_outputs
    assert candidate_input["material"] == material
    snapshot = _chat_input(user_text).routing_catalog
    definition = _registry().resolve(
        outcome.result.candidates[0].tool_id,
        snapshot=snapshot,
    )
    normalized = definition.normalize(candidate_input, prior_normalized_input=None)
    assert isinstance(
        normalized,
        NeedsInputNormalization if missing_fields else ReadyNormalization,
    )
    assert tuple(
        key for key, value in parameters.items() if value is None
    ) == missing_fields or missing_fields == ("material",)
    assert runnable.call_count == 1


def test_legacy_nested_candidate_parameters_cannot_pass_real_tool_normalizer() -> None:
    from materialsagent.infrastructure.llm.deepseek_chat import DeepSeekChatAdapter

    payload = {
        "route": "TOOL_CANDIDATES",
        "candidates": [
            {
                "tool_id": "zta35g_sem_virtual_lab",
                "candidate_input": {
                    "material": "ZTA35G",
                    "candidate_parameters": _COMPLETE_PARAMETERS,
                    "requested_outputs": ["sem_image"],
                },
            }
        ],
    }
    outcome = DeepSeekChatAdapter(
        _config(),
        structured_runnable=_FixtureReplayRunnable("legacy", payload),
    ).orchestrate(_chat_input("legacy"))
    proposal = outcome.result.candidates[0]
    definition = _registry().resolve(
        proposal.tool_id,
        snapshot=_chat_input("legacy").routing_catalog,
    )

    with pytest.raises(ValueError):
        definition.normalize(proposal.candidate_input, prior_normalized_input=None)
