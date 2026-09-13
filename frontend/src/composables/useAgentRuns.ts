import { computed, onUnmounted, ref, watch } from "vue";
import { createMaterialsAgentApi } from "../api/client";
import { agentRequest, AgentRequestError, type AgentRun } from "../api/agent";
import type { ConversationListItem } from "../api/types";

interface PendingOperation {
  path: string;
  body: Record<string, unknown>;
  key: string;
  conversationId: string | null;
  createKey?: string;
}
const PENDING_KEY = "materials-agent.pending-run.v1";
const SELECTED_KEY = "materials-agent.selected-conversation.v1";

export function useAgentRuns() {
  const conversationsApi = createMaterialsAgentApi();
  const conversations = ref<ConversationListItem[]>([]);
  const selectedId = ref<string | null>(sessionStorage.getItem(SELECTED_KEY));
  const runs = ref<AgentRun[]>([]);
  const loading = ref(false);
  const sending = ref(false);
  const completed = ref(0);
  const error = ref<string | null>(null);
  const pending = ref<PendingOperation | null>(null);
  const nextCursor = ref<string | null>(null);
  const conversationCursor = ref<string | null>(null);
  const resumeTarget = ref<AgentRun | null>(null);
  let timer: ReturnType<typeof setInterval> | undefined;
  let polling = false;
  let generation = 0;
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    if (raw) {
      const value = JSON.parse(raw);
      if (typeof value.key === "string" && typeof value.path === "string" && value.body &&
          /^\/(conversations\/[^/]+\/messages|agent-runs\/[^/]+\/(retry|invocations\/[^/]+\/(confirm|reject)))$/.test(value.path)) {
        pending.value = value;
      }
    }
  } catch { error.value = "待提交操作无法读取，请检查当前运行状态。"; }
  const uploading = ref(false);
  const canRetryUpload = ref(false);
  const uploadError = ref<string | null>(null);
  const draftKey = computed(() => `${selectedId.value ?? 'new'}:${resumeTarget.value?.agent_run_id ?? 'new'}`);
  const drafts = ref<Record<string, { text: string; assetId?: string }>>({});
  try {
    const stored = JSON.parse(sessionStorage.getItem("materials-agent.ebsd-drafts.v1") ?? "{}");
    if (stored && typeof stored === "object" && !Array.isArray(stored)) {
      drafts.value = Object.fromEntries(Object.entries(stored).filter(([, value]) => {
        const item = value as { text?: unknown; assetId?: unknown } | null;
        return item && typeof item.text === "string" && (item.assetId === undefined ||
          (typeof item.assetId === "string" && /^asset_[A-Za-z0-9_-]{1,90}$/.test(item.assetId)));
      })) as Record<string, { text: string; assetId?: string }>;
    }
  } catch { /* Discard invalid local drafts. */ }
  const draft = computed(() => drafts.value[draftKey.value] ?? { text: "" });
  function saveDraft(value: { text: string; assetId?: string }) {
    drafts.value[draftKey.value] = value;
    sessionStorage.setItem("materials-agent.ebsd-drafts.v1", JSON.stringify(drafts.value));
  }
  function setDraftText(text: string) { if (!pending.value) saveDraft({ ...draft.value, text }); }
  function removeEbsd() { saveDraft({ text: draft.value.text }); uploadError.value = null; canRetryUpload.value = false; uploadAttempt = null; }
  watch(draftKey, () => { uploadError.value = null; canRetryUpload.value = false; });
  let uploadAttempt: { file: File; key: string; createKey: string; conversationId: string | null } | null = null;
  const busy = computed(() => sending.value || pending.value !== null || uploading.value);

  async function retryEbsdUpload() { if (uploadAttempt) await uploadEbsd(uploadAttempt.file); }

  async function uploadEbsd(file: File) {
    if (busy.value) return;
    uploadError.value = null;
    canRetryUpload.value = false;
    if (!['image/png', 'image/jpeg'].includes(file.type) || file.size === 0 || file.size > 10 * 1024 * 1024) {
      uploadError.value = "请选择不超过 10 MiB 的 PNG/JPEG 图片。";
      return;
    }
    if (!uploadAttempt || uploadAttempt.file !== file || uploadAttempt.conversationId !== selectedId.value) {
      uploadAttempt = { file, key: crypto.randomUUID(), createKey: crypto.randomUUID(), conversationId: selectedId.value };
    }
    const attempt = uploadAttempt;
    const originalDraft = { ...draft.value };
    uploading.value = true;
    try {
      if (!attempt.conversationId) {
        const created = await conversationsApi.createConversation(undefined, attempt.createKey);
        attempt.conversationId = created.data.conversation_id;
        await select(attempt.conversationId, true);
        saveDraft(originalDraft);
        await refreshConversations();
      }
      const context = draftKey.value;
      const response = await fetch(`/api/v1/conversations/${encodeURIComponent(attempt.conversationId)}/ebsd-images`, {
        method: 'POST', headers: { 'Content-Type': file.type, 'Idempotency-Key': attempt.key }, body: file,
      });
      const payload = await response.json();
      if (context !== draftKey.value) return;
      if (!response.ok || !/^asset_[A-Za-z0-9_-]{1,90}$/.test(payload.data?.asset_id ?? '')) {
        throw new Error(response.status === 422 ? "图片必须是边长 128–4096 像素的正方形 RGB PNG/JPEG。" : "上传未完成，请重试原图片。");
      }
      saveDraft({ ...draft.value, assetId: payload.data.asset_id });
      uploadAttempt = null;
    } catch (cause) { canRetryUpload.value = true; uploadError.value = cause instanceof Error && !(cause instanceof TypeError) ? cause.message : "连接中断，请重试原图片。"; }
    finally { uploading.value = false; }
  }


  function persistPending() {
    if (pending.value) sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending.value));
    else sessionStorage.removeItem(PENDING_KEY);
  }

  async function refreshConversations(more = false) {
    const response = await conversationsApi.listConversations(20, more ? conversationCursor.value ?? undefined : undefined);
    conversations.value = more ? [...conversations.value, ...response.data.items] : response.data.items;
    conversationCursor.value = response.data.next_cursor;
  }

  async function refresh(more = false) {
    const id = selectedId.value;
    if (!id || polling) return;
    const epoch = generation;
    polling = true;
    try {
      const query = more && nextCursor.value ? `?before=${encodeURIComponent(nextCursor.value)}` : "";
      const page = await agentRequest<{ items: AgentRun[]; next_cursor: string | null }>(`/conversations/${encodeURIComponent(id)}/agent-runs${query}`);
      if (epoch !== generation || selectedId.value !== id) return;
      const map = new Map(runs.value.map(run => [run.agent_run_id, run]));
      for (const run of page.items) {
        const existing = map.get(run.agent_run_id);
        if (!existing || run.version >= existing.version) map.set(run.agent_run_id, run);
      }
      runs.value = [...map.values()].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.agent_run_id.localeCompare(b.agent_run_id));
      if (more || !nextCursor.value) nextCursor.value = page.next_cursor;
      if (resumeTarget.value) resumeTarget.value = runs.value.find(run => run.agent_run_id === resumeTarget.value?.agent_run_id && run.status === "WAITING_FOR_USER") ?? null;
    } finally { polling = false; }
  }

  async function select(id: string | null, uploadCreation = false) {
    if (uploading.value && !uploadCreation) return;
    generation++;
    selectedId.value = id;
    runs.value = [];
    nextCursor.value = null;
    resumeTarget.value = null;
    if (id) sessionStorage.setItem(SELECTED_KEY, id);
    else sessionStorage.removeItem(SELECTED_KEY);
    loading.value = true;
    try { await refresh(); }
    catch { error.value = "运行记录暂时无法加载，请刷新重试。"; }
    finally { loading.value = false; }
  }

  async function sendPending() {
    if (!pending.value || sending.value) return;
    sending.value = true;
    error.value = null;
    try {
      const operation = pending.value;
      if (operation.conversationId === null) {
        const created = await conversationsApi.createConversation(undefined, operation.createKey);
        operation.conversationId = created.data.conversation_id;
        operation.path = `/conversations/${encodeURIComponent(operation.conversationId)}/messages`;
        persistPending();
        await select(operation.conversationId);
        await refreshConversations();
      }
      const result = await agentRequest<{ agent_run?: AgentRun } | AgentRun>(operation.path, { body: operation.body, key: operation.key });
      const run = "agent_run_id" in result ? result : result.agent_run;
      if (run && selectedId.value === run.conversation_id) {
        runs.value = [...runs.value.filter(item => item.agent_run_id !== run.agent_run_id), run].sort((a, b) => a.created_at.localeCompare(b.created_at));
      }
      pending.value = null;
      if (operation.path.endsWith("/messages")) {
        const context = `${operation.conversationId}:${operation.body.agent_run_id ?? 'new'}`;
        delete drafts.value[context];
        sessionStorage.setItem("materials-agent.ebsd-drafts.v1", JSON.stringify(drafts.value));
        completed.value++;
      }
      persistPending();
      resumeTarget.value = null;
      try { await refresh(); await refreshConversations(); }
      catch { error.value = "提交已保存，列表暂时无法刷新。"; }
    } catch (cause) {
      const uncertain = !(cause instanceof AgentRequestError) || cause.uncertain;
      error.value = uncertain ? "连接中断，执行结果尚不确定。可以检查原提交；这不会创建新的目标。" : `操作未完成（${cause.code}），请刷新状态后重试。`;
      if (!uncertain) { pending.value = null; persistPending(); }
    } finally { sending.value = false; }
  }

  async function submit(text: string) {
    if (busy.value) return;
    const target = resumeTarget.value;
    pending.value = {
      path: `/conversations/${selectedId.value ? encodeURIComponent(selectedId.value) : "new"}/messages`,
      body: target ? { mode: "RESUME_RUN", content_text: text, agent_run_id: target.agent_run_id, waiting_version: target.waiting_version }
        : { mode: "NEW_RUN", content_text: text },
      key: crypto.randomUUID(), conversationId: selectedId.value,
      ...(selectedId.value ? {} : { createKey: crypto.randomUUID() }),
    };
    if (draft.value.assetId) pending.value.body.ebsd_asset_id = draft.value.assetId;
    persistPending();
    await sendPending();
  }

  async function confirm(run: AgentRun, approved: boolean) {
    const execution = run.pending_execution;
    if (busy.value || !execution) return;
    pending.value = { path: `/agent-runs/${run.agent_run_id}/invocations/${execution.invocation_run_id}/${approved ? "confirm" : "reject"}`,
      body: { waiting_version: run.waiting_version, confirmation_version: execution.confirmation_version },
      key: crypto.randomUUID(), conversationId: run.conversation_id };
    persistPending();
    await sendPending();
  }

  async function retry(run: AgentRun, invocationId?: string) {
    if (busy.value) return;
    pending.value = { path: `/agent-runs/${run.agent_run_id}/retry`, body: invocationId
      ? { retry_type: "TOOL_RETRY", invocation_run_id: invocationId } : { retry_type: "ANSWER_REGENERATION" },
      key: crypto.randomUUID(), conversationId: run.conversation_id };
    persistPending();
    await sendPending();
  }

  async function remove(id: string) {
    if (busy.value) return;
    await conversationsApi.deleteConversation?.(id);
    for (const key of Object.keys(drafts.value)) if (key.startsWith(`${id}:`)) delete drafts.value[key];
    sessionStorage.setItem("materials-agent.ebsd-drafts.v1", JSON.stringify(drafts.value));
    if (selectedId.value === id) await select(null);
    await refreshConversations();
  }

  async function initialize() {
    try {
      await refreshConversations();
      if (selectedId.value) await select(selectedId.value);
    } catch { error.value = "无法连接本地服务，请检查服务状态后刷新。"; }
    timer = setInterval(() => { void refresh().catch(() => undefined); }, 3000);
  }
  onUnmounted(() => { if (timer) clearInterval(timer); generation++; });
  return { conversations, selectedId, runs, loading, sending, completed, busy, error, pending, nextCursor, conversationCursor,
    uploading, uploadError, canRetryUpload, draft, setDraftText, uploadEbsd, retryEbsdUpload, removeEbsd,
    resumeTarget, initialize, select, refresh, refreshConversations, submit, sendPending, confirm, retry, remove };
}
