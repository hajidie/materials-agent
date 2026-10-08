import { ref } from "vue";
import type { AgentRun, ProcessSnapshot } from "../api/agent";

export function useProcessStreams(updateRun: (run: AgentRun) => void, settled: () => void) {
  const processes = ref<Record<string, ProcessSnapshot>>({});
  const reconnecting = ref<Record<string, boolean>>({});
  const clients = new Map<string, EventSource>();
  let generation = 0;

  function observe(run: AgentRun) {
    if (clients.has(run.agent_run_id) || typeof EventSource === "undefined" || !["PENDING", "RUNNING"].includes(run.status)) return;
    const id = run.agent_run_id, epoch = generation;
    const source = new EventSource(`/api/v1/agent-runs/${encodeURIComponent(id)}/events`);
    clients.set(id, source);
    const receive = (event: MessageEvent, replace: boolean) => {
      if (epoch !== generation || clients.get(id) !== source) return;
      try {
        const data = JSON.parse(event.data) as ProcessSnapshot;
        if (data.run.agent_run_id !== id || !Array.isArray(data.segments)) return;
        const old = processes.value[id];
        const reset = replace || !old || old.epoch !== data.epoch;
        const segments = new Map((reset ? [] : old.segments).map(s => [s.segment_id, s]));
        for (const segment of data.segments) {
          if (!segments.has(segment.segment_id) || segment.revision >= segments.get(segment.segment_id)!.revision) segments.set(segment.segment_id, segment);
        }
        processes.value[id] = { ...data, segments: [...segments.values()].sort((a, b) => a.sequence - b.sequence) };
        reconnecting.value[id] = false;
        updateRun(data.run);
      } catch { reconnecting.value[id] = true; }
    };
    source.addEventListener("snapshot", event => receive(event as MessageEvent, true));
    source.addEventListener("process.updated", event => receive(event as MessageEvent, false));
    source.addEventListener("settled", () => {
      if (clients.get(id) !== source) return;
      source.close(); clients.delete(id); reconnecting.value[id] = false; settled();
    });
    source.onerror = () => { if (clients.get(id) === source) reconnecting.value[id] = true; };
  }

  function reset() {
    generation++;
    for (const source of clients.values()) source.close();
    clients.clear(); processes.value = {}; reconnecting.value = {};
  }
  return { processes, reconnecting, observe, reset };
}
