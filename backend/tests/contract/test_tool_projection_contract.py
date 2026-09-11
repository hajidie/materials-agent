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
        "InvocationToolView": {
            "tool_id",
            "version",
            "display_name",
            "execution_profile",
            "confirmation_required",
            "confirmation_prompt",
        },
        "InvocationDataView": {
            "invocation_run_id",
            "conversation_id",
            "source_message_id",
            "task_id",
            "status",
            "tool",
            "confirmation_required",
            "confirmation_expires_at",
            "confirmed_at",
            "rejected_at",
            "expired_at",
            "dispatch_started_at",
            "error_code",
            "safe_error_message",
            "result",
            "created_at",
            "updated_at",
            "completed_at",
        },
    }
    for name, fields in expected_fields.items():
        assert components[name]["additionalProperties"] is False
        assert set(components[name]["properties"]) == fields

    serialized = str(
        {
            name: components[name]
            for name in ("PublicToolCatalogItemView", "InvocationToolView")
        }
    )
    assert "schema_hash" not in serialized
    assert "required_permissions" not in serialized
    assert "runtime_metadata" not in serialized
    assert "execution_target" not in serialized
