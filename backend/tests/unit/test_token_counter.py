from __future__ import annotations

from materialsagent.infrastructure.llm.token_counter import Cl100kTokenCounter


def test_token_counter_uses_conservative_utf8_fallback_offline(monkeypatch) -> None:
    def unavailable(_name: str) -> object:
        raise OSError("controlled offline failure")

    monkeypatch.setattr(
        "materialsagent.infrastructure.llm.token_counter.tiktoken.get_encoding",
        unavailable,
    )

    counter = Cl100kTokenCounter()

    assert counter.count_text("A钛") == len("A钛".encode("utf-8"))
    assert counter.count_messages([{"role": "user", "content": "钛"}]) == 13
