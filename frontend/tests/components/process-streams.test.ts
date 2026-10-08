import { afterEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { useProcessStreams } from "../../src/composables/useProcessStreams";
import ResearchProcess from "../../src/components/ResearchProcess.vue";
import type { AgentRun, ProcessSnapshot } from "../../src/api/agent";

class Source extends EventTarget {
  static all: Source[] = [];
  closed = false;
  onerror: (() => void) | null = null;
  constructor(public url: string) { super(); Source.all.push(this); }
  close() { this.closed = true; }
  emit(type: string, data: unknown) { this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify(data) })); }
}
const run = { agent_run_id: "stream-unit", status: "RUNNING", version: 1 } as AgentRun;
const snapshot = (revision = 1, text = "分析条件"): ProcessSnapshot => ({ epoch: "server", revision, run,
  segments: [{ segment_id: "reason", sequence: 0, revision, kind: "reasoning", purpose: "process", status: "streaming", text }] });
afterEach(() => { vi.unstubAllGlobals(); Source.all = []; });

it('reconnects with snapshots without duplicate segments or a new execution request', () => {
  vi.stubGlobal('EventSource', Source);
  const update = vi.fn(), settled = vi.fn();
  const state = useProcessStreams(update, settled);
  state.observe(run); state.observe(run);
  expect(Source.all).toHaveLength(1);
  const source = Source.all[0]!;
  source.emit('snapshot', snapshot());
  source.onerror?.(); expect(state.reconnecting.value[run.agent_run_id]).toBe(true);
  source.emit('snapshot', snapshot(2, '分析条件完成'));
  source.emit('process.updated', snapshot(1, '迟到的旧内容'));
  expect(state.processes.value[run.agent_run_id]!.segments).toHaveLength(1);
  expect(state.processes.value[run.agent_run_id]!.segments[0]!.text).toBe('分析条件完成');
  source.emit('settled', {});
  expect(source.closed).toBe(true); expect(settled).toHaveBeenCalledOnce();
  state.reset();
});

it('ignores messages from a connection closed by changing conversations', () => {
  vi.stubGlobal('EventSource', Source);
  const update = vi.fn();
  const state = useProcessStreams(update, vi.fn());
  state.observe(run); const old = Source.all[0]!;
  state.reset(); old.emit('snapshot', snapshot());
  expect(update).not.toHaveBeenCalled(); expect(state.processes.value).toEqual({});
  expect(old.closed).toBe(true);
});

it('collapses at completion by default and preserves an explicit choice across remount', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({data: snapshot()}))));
  const w = mount(ResearchProcess, { props: { runId: run.agent_run_id, snapshot: snapshot(), active: true } });
  await flushPromises(); expect(w.get('details').attributes('open')).toBeDefined();
  await w.setProps({active: false}); expect(w.get('details').attributes('open')).toBeUndefined();
  await w.get('summary').trigger('click'); expect(w.text()).toContain('模型思考');
  w.unmount();
  const finished = mount(ResearchProcess, { props: { runId: run.agent_run_id, snapshot: snapshot(), active: false } });
  await flushPromises(); expect(finished.get('details').attributes('open')).toBeDefined();
  await finished.get('summary').trigger('click'); expect(finished.find('.research-process__body').exists()).toBe(false);
});
