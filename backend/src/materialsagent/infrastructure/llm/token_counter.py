from __future__ import annotations

import json

import tiktoken


class _Utf8ByteEncoding:
    """Conservative offline fallback: BPE cannot use more tokens than bytes."""

    @staticmethod
    def encode(value: str) -> list[int]:
        return list(value.encode("utf-8"))


class Cl100kTokenCounter:
    """Deterministic provider-neutral prompt token approximation."""

    def __init__(self) -> None:
        try:
            self._encoding = tiktoken.get_encoding("cl100k_base")
        except OSError:
            # tiktoken downloads its vocabulary on first use. Local/offline
            # startup must remain available when that cache is absent; byte
            # counting is deterministic and conservatively overestimates BPE.
            self._encoding = _Utf8ByteEncoding()

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
