from __future__ import annotations

from types import MappingProxyType

from materialsagent.api.routes.tool_results import _public_json
from materialsagent.application.result_service import ResultArtifactProjection


def test_artifact_projection_has_only_public_fields_and_stable_content_url() -> None:
    artifact = ResultArtifactProjection(
        asset_id="asset_1",
        status="AVAILABLE",
        role="requested_output",
        media_type="image/png",
        width=512,
        height=512,
        bit_depth=8,
        size_bytes=1480,
        sha256="a" * 64,
        content_url="/api/v1/assets/asset_1/content",
    )

    assert set(artifact.__dataclass_fields__) == {
        "asset_id",
        "status",
        "role",
        "media_type",
        "width",
        "height",
        "bit_depth",
        "size_bytes",
        "sha256",
        "content_url",
    }
    assert "object_key" not in artifact.__dataclass_fields__
    assert "bucket" not in artifact.__dataclass_fields__


def test_public_json_thaws_nested_safe_facts_without_adding_internal_data() -> None:
    value = MappingProxyType(
        {
            "process_parameters": MappingProxyType(
                {"solution_temperature": 1000}
            ),
            "outputs": ("sem_image",),
        }
    )

    assert _public_json(value) == {
        "process_parameters": {"solution_temperature": 1000},
        "outputs": ["sem_image"],
    }
