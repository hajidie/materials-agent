from __future__ import annotations

import argparse

import pytest

from materialsagent.maintenance.cleanup_conversation_objects import _bounded_limit


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("100", 100)])
def test_cleanup_command_accepts_only_the_bounded_range(
    raw: str,
    expected: int,
) -> None:
    assert _bounded_limit(raw) == expected


@pytest.mark.parametrize("raw", ["0", "101", "invalid"])
def test_cleanup_command_rejects_unbounded_or_invalid_limits(raw: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _bounded_limit(raw)
