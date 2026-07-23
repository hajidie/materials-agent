from __future__ import annotations

import inspect


def test_explanation_service_public_surface_has_no_retry_api() -> None:
    from materialsagent.application.explanation_service import (
        ExplanationService,
    )

    public_methods = {
        name
        for name, value in inspect.getmembers(
            ExplanationService,
            predicate=inspect.isfunction,
        )
        if not name.startswith("_")
    }

    assert public_methods == {"explain", "prepare", "finalize"}
