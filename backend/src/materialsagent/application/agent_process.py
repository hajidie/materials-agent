"""Public process content. SDK checkpoints and business state remain separate."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import time
from typing import Any

from materialsagent.domain.models.agent import identifier
from .context_framework import protect_text


class PublicText:
    """Withhold possible private-value prefixes across provider chunk boundaries."""
    def __init__(self, private: set[str]):
        self.private = private
        self.long = sorted((s for s in private if len(s) >= 12), key=len, reverse=True)
        self.short = [s for s in private if len(s) < 12]
        self.pending = ""
        self.text = ""

    def push(self, value: str) -> str:
        self.pending += value
        self.pending = protect_text(self.pending, set(self.long))
        if not self.text.strip() and any(s.startswith(self.pending.strip()) for s in self.short):
            return ""
        keep = 0
        for secret in self.long:
            for size in range(min(len(secret) - 1, len(self.pending)), keep, -1):
                if self.pending.endswith(secret[:size]):
                    keep = size
                    break
        end = len(self.pending) - keep
        delta, self.pending = self.pending[:end], self.pending[end:]
        self.text += delta
        return delta

    def finish(self, *, incomplete: bool = False) -> str:
        tail = protect_text(self.pending, self.private)
        if incomplete and tail and any(secret.startswith(tail) for secret in self.long):
            tail = "内部引用已隐藏"
        if not self.text.strip() and self.pending.strip() in self.private:
            tail = "内部引用已隐藏"
        self.pending = ""
        self.text += tail
        return self.text


@dataclass
class Channel:
    epoch: str = field(default_factory=identifier)
    revision: int = 0
    saved: int = 0
    last_save: float = 0
    segments: dict[str, dict[str, Any]] = field(default_factory=dict)
    listeners: set[asyncio.Queue] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgentProcess:
    """One event-loop owner; bounded notifications coalesce slow subscribers.

    Every reconnect starts with an authoritative content snapshot. We deliberately
    do not keep a second, token-sized event log for historical replay.
    """
    def __init__(self, store):
        self.store = store
        self.channels: dict[str, Channel] = {}
        self.loading = asyncio.Lock()

    async def channel(self, run_id: str, actor_id: str) -> Channel:
        async with self.loading:
            if run_id not in self.channels:
                data = await asyncio.to_thread(self.store.load_process, run_id, actor_id)
                self.channels[run_id] = Channel(revision=data["revision"], saved=data["revision"],
                    segments={s["segment_id"]: s for s in data["segments"]})
            return self.channels[run_id]

    def notify(self, channel: Channel) -> None:
        for queue in tuple(channel.listeners):
            if not queue.full():
                queue.put_nowait(True)

    async def upsert(self, run_id: str, actor_id: str, value: dict, *, flush: bool = False):
        channel = await self.channel(run_id, actor_id)
        async with channel.lock:
            previous = channel.segments.get(value["segment_id"])
            if previous and value.get("state_version", 0) < previous.get("state_version", 0):
                return
            if previous and all(previous.get(k) == v for k, v in value.items()):
                return
            # Late SDK deltas cannot reopen a settled message.
            if previous and value["kind"] in {"text", "reasoning"} and previous["status"] != "streaming" and value["status"] == "streaming":
                return
            content = {**value, "sequence": previous["sequence"] if previous else len(channel.segments),
                       "revision": channel.revision + 1}
            channel.revision += 1
            channel.segments[value["segment_id"]] = content
            self.notify(channel)
            if flush or time.monotonic() - channel.last_save >= 1:
                await self._save(run_id, actor_id, channel)

    async def _save(self, run_id: str, actor_id: str, channel: Channel):
        if channel.saved == channel.revision:
            return
        document = {"revision": channel.revision, "segments": deepcopy(list(channel.segments.values()))}
        await asyncio.to_thread(self.store.save_process, run_id, actor_id, document)
        channel.saved = channel.revision
        channel.last_save = time.monotonic()

    async def finish(self, run_id: str, actor_id: str, status: str, *, final_message_id: str | None = None):
        channel = await self.channel(run_id, actor_id)
        async with channel.lock:
            for segment in channel.segments.values():
                changed = False
                if segment["status"] == "streaming":
                    segment["status"] = "interrupted" if status in {"INTERRUPTED", "TERMINATED"} else "complete"
                    changed = True
                if segment.get("purpose") == "pending":
                    segment["purpose"] = "answer" if final_message_id else "process"
                    segment["message_id"] = final_message_id
                    changed = True
                if changed:
                    channel.revision += 1
                    segment["revision"] = channel.revision
            await self._save(run_id, actor_id, channel)
            self.notify(channel)

    async def snapshot(self, run_id: str, actor_id: str) -> dict:
        # Ownership is checked even when a channel is cached.
        run = await asyncio.to_thread(self.store.get, run_id, actor_id)
        channel = await self.channel(run_id, actor_id)
        async with channel.lock:
            return {"epoch": channel.epoch, "revision": channel.revision,
                    "segments": deepcopy(list(channel.segments.values())), "run": run}

    def changed(self, run_id: str):
        if run_id in self.channels:
            self.notify(self.channels[run_id])

    async def tools(self, run):
        from .result_projection import project_result
        from .context_framework import internal_values
        private = internal_values(run.model_dump(mode="json"))
        records = [*run.executions, *([run.pending_execution] if run.pending_execution else [])]
        for record in records:
            observation = next((o for o in run.observations if o.observation_id == record.observation_id), None)
            presentation = protect_text(project_result(observation), private) if observation and observation.kind == "TOOL_RESULT" else None
            await self.upsert(run.agent_run_id, run.actor_id, {
                "segment_id": f"tool:{record.tool_call_id}", "kind": "tool", "purpose": "process",
                "status": "streaming" if record.status in {"PENDING", "RUNNING", "PENDING_CONFIRMATION"} else "complete",
                "text": "", "tool_name": record.tool_name, "tool_status": record.status,
                "presentation": presentation, "state_version": run.version,
            }, flush=True)

    def evict(self):
        # Completed, saved, unsubscribed channels can always be reloaded from DB.
        for run_id, channel in list(self.channels.items()):
            if len(self.channels) <= 128:
                break
            if not channel.listeners and channel.saved == channel.revision and all(
                s["status"] != "streaming" for s in channel.segments.values()
            ):
                del self.channels[run_id]
