from __future__ import annotations

import json

import tiktoken


class Cl100kTokenCounter:
    """Deterministic provider-neutral prompt token approximation."""

    def __init__(self) -> None:
        self._encoding = tiktoken.get_encoding("cl100k_base")

    def count_text(self, value: str) -> int:
        return len(self._encoding.encode(value))

    def count_messages(self, messages: list[dict[str, str]]) -> int:
        total = 2
        for message in messages:
            total += 4
            total += self.count_text(message["role"])
            total += self.count_text(message["content"])
        return total

    def count_schema(self, schema: object) -> int:
        """Count the structured-output contract included with a model request."""
        payload = json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return self.count_text(payload)
