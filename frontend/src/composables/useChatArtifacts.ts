import { onUnmounted, ref, watch, type Ref } from "vue";
import { agentRequest, type AgentRun } from "../api/agent";
import type { ResultMessage } from "../api/artifacts";

/** Conversation observation is independent of the viewer, and never executes tools. */
export function useChatArtifacts(conversation: Ref<string | null>, runs: Ref<AgentRun[]>) {
  const messages = ref<ResultMessage[]>([]), pending = ref(false), notice = ref<string | null>(null), fence = ref<string | null>(null);
  let epoch = 0, active = false, ready = false, timer: ReturnType<typeof setTimeout> | undefined, deadline = 0;
  let cursor: string | null = null;
  async function refreshState() {
    const id = conversation.value, version = epoch;
    if (!id) return;
    try {
      const state = await agentRequest<{ fence: { operation_id: string | null } }>(`/conversations/${encodeURIComponent(id)}/ml/ui-state`);
      if (version === epoch) fence.value = state.fence.operation_id;
    } catch { /* Submission endpoints remain authoritative for deletion fences. */ }
  }
  async function observe(reset = false) {
    if (reset) deadline = Date.now() + 5 * 60_000;
    const id = conversation.value, version = epoch;
    if (!id || !ready || active || fence.value || document.visibilityState === "hidden") return;
    if (timer) clearTimeout(timer);
    active = true;
    try {
      const result = await agentRequest<{ messages: ResultMessage[]; pending: boolean; next_cursor: string | null }>(`/conversations/${encodeURIComponent(id)}/results/reconcile`, { body: { cursor } });
      if (version !== epoch) return;
      messages.value = result.messages; pending.value = result.pending; notice.value = null;
      cursor = result.next_cursor;
      if (result.pending && Date.now() < deadline) timer = setTimeout(() => { void observe(); }, 5000);
      else if (result.pending) notice.value = "处理仍在继续，可以稍后核查结果。";
    } catch {
      if (version === epoch) notice.value = "暂时无法核查结果。原操作不会重复执行。";
    } finally { if (version === epoch) active = false; }
  }
  watch(conversation, async id => {
    epoch++; cursor = null; active = false; ready = false; if (timer) clearTimeout(timer);
    messages.value = []; pending.value = false; notice.value = null; fence.value = null;
    const version = epoch;
    if (!id) return;
    try {
      const result = await agentRequest<{ items: ResultMessage[] }>(`/conversations/${encodeURIComponent(id)}/result-messages`);
      if (version === epoch) messages.value = result.items;
    } catch { /* The bounded observation below also loads durable messages. */ }
    await refreshState();
    if (version === epoch) { ready = true; await observe(true); }
  }, { immediate: true });
  watch(() => runs.value.filter(r => r.status === "SUCCEEDED" || r.status === "TERMINATED").map(r => r.agent_run_id).join(","), () => {
    if (timer) clearTimeout(timer); void observe(true);
  });
  function visibilityChanged() {
    if (timer) clearTimeout(timer);
    if (document.visibilityState !== "hidden") void observe(true);
  }
  document.addEventListener("visibilitychange", visibilityChanged);
  onUnmounted(() => { epoch++; if (timer) clearTimeout(timer); document.removeEventListener("visibilitychange", visibilityChanged); });
  return { messages, pending, notice, fence, refreshState, observe };
}
