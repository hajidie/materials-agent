from __future__ import annotations

from materialsagent.infrastructure.config import load_settings
from materialsagent.main import create_app


def test_openapi_exposes_strict_discriminated_timeline_contract() -> None:
    schema = create_app(
        settings=load_settings(
            {
                "LOCAL_ACTOR_ID": "actor_contract",
            }
        )
    ).openapi()

    operation = schema["paths"][
        "/api/v1/conversations/{conversation_id}/timeline"
    ]["get"]
    assert {
        parameter["name"] for parameter in operation["parameters"]
    } == {"conversation_id", "limit", "cursor"}
    response_schema = operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema["$ref"].endswith("/TimelineResponse")

    components = schema["components"]["schemas"]
    for name in (
        "TimelineResponse",
        "TimelineDataView",
        "UserMessageTimelineItemView",
        "AssistantMessageTimelineItemView",
        "ToolInvocationTimelineItemView",
        "ToolTaskTimelineItemView",
    ):
        assert components[name]["additionalProperties"] is False

    items = components["TimelineDataView"]["properties"]["items"]["items"]
    assert items["discriminator"]["propertyName"] == "item_type"
    assert set(items["discriminator"]["mapping"]) == {
        "USER_MESSAGE",
        "ASSISTANT_MESSAGE",
        "TOOL_INVOCATION",
        "TOOL_TASK",
    }
