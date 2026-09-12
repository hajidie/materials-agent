from __future__ import annotations

from materialsagent.infrastructure.config import load_settings
from materialsagent.main import create_app


def test_catalog_and_invocation_openapi_use_strict_safe_projection_dtos() -> None:
    schema = create_app(
        settings=load_settings({"LOCAL_ACTOR_ID": "actor_projection_contract"})
    ).openapi()
    components = schema["components"]["schemas"]

    expected_fields = {
        "PublicToolCatalogItemView": {
            "tool_id",
            "display_name",
            "description",
            "status",
            "execution_profile",
            "confirmation_required",
            "supported_outputs",
            "limitations",
            "availability",
        },
    }
    for name, fields in expected_fields.items():
        assert components[name]["additionalProperties"] is False
        assert set(components[name]["properties"]) == fields

    serialized = str(
        {
            name: components[name]
            for name in ("PublicToolCatalogItemView",)
        }
    )
    assert "schema_hash" not in serialized
    assert "required_permissions" not in serialized
    assert "runtime_metadata" not in serialized
    assert "execution_target" not in serialized

    assert components["AgentRunView"]["additionalProperties"] is False
    assert not {"actor_id", "claim", "process_id", "context"}.intersection(components["AgentRunView"]["properties"])
    assert "Submission" in components and "CallTool" in components and "Finish" in components
