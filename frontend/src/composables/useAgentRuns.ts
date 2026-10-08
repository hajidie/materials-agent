import { computed, onUnmounted, ref, watch } from "vue";
import { createMaterialsAgentApi } from "../api/client";
import { agentRequest, AgentRequestError, type AgentRun, type ChatMessage, type AcceptedSubmission } from "../api/agent";
import type { Attachment } from "../api/artifacts";
import type { ConversationListItem } from "../api/types";
import { useProcessStreams } from "./useProcessStreams";

interface PendingOperation {
  path: string;
  body: Record<string, unknown>;
  key: string;
  conversationId: string | null;
  createKey?: string;
  draftKey?: string;
  accepted?: { runId: string; submissionId: string };
  stopRequested?: boolean;
}
const PENDING_KEY = "materials-agent.pending-run.v3";
const SELECTED_KEY = "materials-agent.selected-conversation.v1";

export function useAgentRuns() {
  // Incompatible drafts must never replay the former message contract.
  sessionStorage.removeItem("materials-agent.pending-run.v1");
  sessionStorage.removeItem("materials-agent.pending-run.v2");
  sessionStorage.removeItem("materials-agent.attachments-drafts.v2");
  sessionStorage.removeItem("materials-agent.ebsd-drafts.v1");
  const conversationsApi = createMaterialsAgentApi();
  const conversations = ref<ConversationListItem[]>([]);
  const selectedId = ref<string | null>(sessionStorage.getItem(SELECTED_KEY));
  const runs = ref<AgentRun[]>([]);
  const messages = ref<ChatMessage[]>([]);
  const stopping = ref(false);
  let stopPromise: Promise<void> | null = null;
  const loading = ref(false);
  const sending = ref(false);
  const completed = ref(0);
  const error = ref<string | null>(null);
  const pending = ref<PendingOperation | null>(null);
  const writeBlocked = ref(false);
  const nextCursor = ref<string | null>(null);
  const conversationCursor = ref<string | null>(null);
  const resumeTarget = ref<AgentRun | null>(null);
  let timer: ReturnType<typeof setInterval> | undefined;
  let polling = false;
  let generation = 0;
  function updateRun(run: AgentRun) {
    if (run.conversation_id !== selectedId.value) return;
    const previous = runs.value.find(r => r.agent_run_id === run.agent_run_id);
    if (previous && previous.version > run.version) return;
    runs.value = [...runs.value.filter(r => r.agent_run_id !== run.agent_run_id), run];
    resumeTarget.value = runs.value.find(r => r.status === "WAITING_FOR_USER") ?? null;
  }
  const streams = useProcessStreams(updateRun, () => { void refresh().catch(() => undefined); });
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    if (raw) {
      const value = JSON.parse(raw);
      if (typeof value.key === "string" && typeof value.path === "string" && value.body &&
          /^\/(conversations\/[^/]+\/messages|messages\/[^/]+\/regenerate|agent-runs\/[^/]+\/(resume|retry|invocations\/[^/]+\/(confirm|reject)))$/.test(value.path)) {
        pending.value = value;
      }
    }
  } catch { error.value = "待提交操作无法读取，请检查当前处理状态。"; }
  const uploading = ref(false);
  const canRetryUpload = ref(false);
  const uploadError = ref<string | null>(null);
  const draftKey = computed(() => `${selectedId.value ?? 'new'}:${resumeTarget.value?.agent_run_id ?? 'new'}`);
  const DRAFT_KEY = "materials-agent.attachments-drafts.v3";
  const drafts = ref<Record<string, { text: string; attachment?: Attachment }>>({});
  try {
    const stored = JSON.parse(sessionStorage.getItem(DRAFT_KEY) ?? "{}");
    if (stored && typeof stored === "object" && !Array.isArray(stored)) {
      for (const [key, value] of Object.entries(stored)) {
        const item = value as { text?: unknown; attachment?: Attachment };
        if (typeof item?.text === "string" && (!item.attachment ||
            (typeof item.attachment.attachment_id === "string" && typeof item.attachment.name === "string" &&
              ["dataset", "ebsd_image"].includes(item.attachment.kind)))) drafts.value[key] = item as { text: string; attachment?: Attachment };
      }
    }
  } catch { /* Discard invalid local drafts. */ }
  const draft = computed(() => drafts.value[draftKey.value] ?? { text: "" });
  function saveDraft(value: { text: string; attachment?: Attachment }, key = draftKey.value) {
    drafts.value[key] = value;
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(drafts.value));
  }
  function setDraftText(text: string) { if (!pending.value) saveDraft({ ...draft.value, text }); }
  function removeAttachment() { saveDraft({ text: draft.value.text }); uploadError.value = null; canRetryUpload.value = false; delete uploadAttempts[draftKey.value]; persistUploads(); }
  const UPLOAD_KEY = "materials-agent.pending-upload.v2";
  type UploadAttempt = { key: string; kind: "dataset" | "ebsd_image"; name: string; createKey: string; conversationId: string | null; context: string };
  const uploadAttempts: Record<string, UploadAttempt> = {};
  function persistUploads() { sessionStorage.setItem(UPLOAD_KEY, JSON.stringify(uploadAttempts)); }
  try {
    const values = JSON.parse(sessionStorage.getItem(UPLOAD_KEY) ?? "{}");
    for (const value of Object.values(values) as UploadAttempt[]) {
      if (value && typeof value.key === "string" && typeof value.name === "string" && typeof value.context === "string" && ["dataset", "ebsd_image"].includes(value.kind)) uploadAttempts[value.context] = value;
    }
  } catch { /* Invalid pending uploads cannot be replayed. */ }
  function showPendingUpload() {
    canRetryUpload.value = Boolean(uploadAttempts[draftKey.value]);
    uploadError.value = canRetryUpload.value ? "上传结果尚未确认，请核查原请求。" : null;
  }
  watch(draftKey, showPendingUpload, { immediate: true });
  const generating = computed(() => sending.value || stopping.value || runs.value.some(r => ["PENDING", "RUNNING"].includes(r.status)));
  const busy = computed(() => generating.value || pending.value !== null || uploading.value || writeBlocked.value);

  async function checkUpload() {
    const attempt = uploadAttempts[draftKey.value];
    if (!attempt?.conversationId || busy.value) return;
    uploading.value = true;
    try {
      const result = await agentRequest<{ attachment: Attachment | null; message: string }>(`/conversations/${encodeURIComponent(attempt.conversationId)}/attachments/reconcile`,
        { body: { key: attempt.key, kind: attempt.kind, name: attempt.name } });
      if (attempt.context !== draftKey.value) return;
      if (result.attachment) {
        saveDraft({ ...draft.value, attachment: result.attachment }); delete uploadAttempts[attempt.context]; persistUploads(); uploadError.value = null; canRetryUpload.value = false;
      } else uploadError.value = result.message;
    } catch { uploadError.value = "仍无法确认上传结果，请稍后核查原请求。"; }
    finally { uploading.value = false; }
  }

  async function uploadAttachment(file: File) {
    if (busy.value) return;
    if (uploadAttempts[draftKey.value]) { showPendingUpload(); return; }
    uploadError.value = null; canRetryUpload.value = false;
    const isCsv = file.name.toLowerCase().endsWith(".csv");
    if ((!isCsv && !['image/png', 'image/jpeg'].includes(file.type)) || !file.size || file.size > (isCsv ? 20 : 10) * 1024 * 1024) {
      uploadError.value = "请选择 CSV（不超过 20 MiB）或 PNG/JPEG 图片（不超过 10 MiB）。"; return;
    }
    const attempt: UploadAttempt = { key: crypto.randomUUID(), createKey: crypto.randomUUID(), conversationId: selectedId.value,
      name: file.name, kind: isCsv ? "dataset" : "ebsd_image", context: draftKey.value };
    const originalDraft = { ...draft.value };
    uploading.value = true;
    try {
      if (!attempt.conversationId) {
        const created = await conversationsApi.createConversation(undefined, attempt.createKey);
        attempt.conversationId = created.data.conversation_id;
        await select(attempt.conversationId, true); saveDraft(originalDraft); await refreshConversations();
      }
      attempt.context = draftKey.value;
      uploadAttempts[attempt.context] = attempt; persistUploads();
      const form = new FormData(); form.append("file", file);
      const response = await fetch(`/api/v1/conversations/${encodeURIComponent(attempt.conversationId)}/attachments`, {
        method: 'POST', headers: { 'Idempotency-Key': attempt.key }, body: form,
      });
      const payload = await response.json();
      if (attempt.context !== draftKey.value) return;
      if (!response.ok) {
        if (response.status >= 400 && response.status < 500) { delete uploadAttempts[attempt.context]; persistUploads(); }
        throw new Error();
      }
      if (!payload.data?.attachment) { showPendingUpload(); return; }
      saveDraft({ ...draft.value, attachment: payload.data.attachment }); delete uploadAttempts[attempt.context]; persistUploads();
    } catch {
      canRetryUpload.value = Boolean(uploadAttempts[draftKey.value]);
      uploadError.value = uploadAttempts[draftKey.value] ? "上传结果尚未确认，请核查原请求。" : "上传未完成，请检查文件格式。图片需为边长 128–4096 像素的正方形 RGB 图片。";
    } finally { uploading.value = false; }
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
      const messageBefore = more ? nextCursor.value : null;
      const query = "";
      const page = await agentRequest<{ items: AgentRun[]; next_cursor: string | null }>(`/conversations/${encodeURIComponent(id)}/agent-runs${query}`);
      if (epoch !== generation || selectedId.value !== id) return;
      const map = new Map(runs.value.map(run => [run.agent_run_id, run]));
      for (const run of page.items) {
        const existing = map.get(run.agent_run_id);
        if (!existing || run.version >= existing.version) map.set(run.agent_run_id, run);
      }
      runs.value = [...map.values()].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.agent_run_id.localeCompare(b.agent_run_id));
      resumeTarget.value = runs.value.find(run => run.status === "WAITING_FOR_USER") ?? null;
      for (const run of runs.value) streams.observe(run);
      const messageQuery = messageBefore ? `?before=${encodeURIComponent(messageBefore)}` : "";
      const messagePage = await agentRequest<{ items: ChatMessage[]; next_cursor: number | null }>(`/conversations/${encodeURIComponent(id)}/messages${messageQuery}`);
      if (epoch !== generation || selectedId.value !== id) return;
      const groups = new Map(messages.value.map(m => [m.answer_root_message_id ?? m.message_id, m]));
      for (const message of messagePage.items) groups.set(message.answer_root_message_id ?? message.message_id, message);
      messages.value = [...groups.values()].sort((a, b) => a.sequence - b.sequence);
      if (more || nextCursor.value === null) nextCursor.value = messagePage.next_cursor === null ? null : String(messagePage.next_cursor);
    } finally { polling = false; }
  }

  async function select(id: string | null, uploadCreation = false) {
    if (uploading.value && !uploadCreation) return;
    generation++;
    streams.reset();
    selectedId.value = id;
    runs.value = [];
    messages.value = [];
    nextCursor.value = null;
    resumeTarget.value = null;
    if (id) sessionStorage.setItem(SELECTED_KEY, id);
    else sessionStorage.removeItem(SELECTED_KEY);
    loading.value = true;
    try { await refresh(); }
    catch { error.value = "对话暂时无法加载，请刷新重试。"; }
    finally { loading.value = false; }
  }

  async function stop() {
    if (stopPromise) return stopPromise;
    const operation = pending.value;
    const run = runs.value.find(r => ["PENDING", "RUNNING", "INTERRUPTED"].includes(r.status));
    if (!operation && !run) return;
    if (operation) { operation.stopRequested = true; persistPending(); }
    stopping.value = true;
    const target = operation?.accepted ?? (run?.submission_id ? { runId: run.agent_run_id, submissionId: run.submission_id } : null);
    if (!target) return; // sendPending sends the stop as soon as acceptance is known.
    stopPromise = (async () => {
      try {
        const stopped = await agentRequest<{ agent_run: AgentRun }>(`/agent-runs/${encodeURIComponent(target.runId)}/stop`, { body: { submission_id: target.submissionId } });
        if (stopped.agent_run.conversation_id === selectedId.value) {
          runs.value = [...runs.value.filter(r => r.agent_run_id !== stopped.agent_run.agent_run_id), stopped.agent_run];
        }
        if (pending.value === operation) { pending.value = null; persistPending(); }
        sending.value = false;
        await refresh();
      } catch {
        error.value = "暂时无法确认中止结果，请再次点击中止核查。";
        await refresh().catch(() => undefined);
      } finally { stopping.value = false; stopPromise = null; }
    })();
    return stopPromise;
  }

  async function sendPending() {
    if (!pending.value || sending.value) return;
    const operation = pending.value;
    const submittedDraftKey = operation.draftKey ?? draftKey.value;
    sending.value = true; error.value = null;
    try {
      if (!operation.conversationId) {
        const created = await conversationsApi.createConversation(undefined, operation.createKey);
        operation.conversationId = created.data.conversation_id;
        operation.path = `/conversations/${operation.conversationId}/messages`;
        persistPending(); await select(operation.conversationId);
      }
      if (!operation.accepted) {
        const result = await agentRequest<AcceptedSubmission | AgentRun>(operation.path, { body: operation.body, key: operation.key });
        if (pending.value !== operation) return;
        const acceptedRun = "agent_run" in result ? result.agent_run : result;
        operation.accepted = { runId: acceptedRun.agent_run_id, submissionId: result.submission_id ?? acceptedRun.submission_id! };
        updateRun(acceptedRun);
        streams.observe(acceptedRun);
        persistPending();
        if (operation.path.endsWith("/messages")) { saveDraft({ text: "" }, submittedDraftKey); completed.value++; }
      }
      if (operation.stopRequested) { await stop(); return; }
      if (pending.value === operation) { pending.value = null; persistPending(); }
      await refresh(); await refreshConversations();
    } catch (cause) {
      if (pending.value !== operation) return;
      stopping.value = false;
      if (operation.stopRequested) error.value = "尚未确认原提交，请检查原提交后继续中止。";
      if (!operation.stopRequested) {
        const uncertain = !(cause instanceof AgentRequestError) || cause.uncertain;
        error.value = uncertain ? "请求结果尚未确认，请核查原提交。" : "请求未被接受，请检查当前对话后重试。";
        if (cause instanceof AgentRequestError && operation.path.endsWith('/resume')) {
          if (cause.code === 'TOOL_OUTCOME_UNKNOWN') error.value = "原计算的结果尚未确认，请稍后再点继续处理；已提交的计算不会重复执行。";
          if (cause.code === 'CHECKPOINT_MISSING') error.value = "恢复所需的运行记录缺失。已保存的内容仍可查看，请结束本次处理后重新提交。";
          if (cause.code === 'LLM_RETRY_NOT_READY') error.value = "模型服务要求稍后再试，请等待后继续处理。";
          if (cause.code === 'AGENT_RECOVERY_BUDGET_EXCEEDED') error.value = "本次处理已达到额度上限，请结束本次处理。";
        }
        if (!uncertain && pending.value === operation) { pending.value = null; persistPending(); }
      }
    } finally {
      if (!pending.value || pending.value === operation) sending.value = false;
    }
  }

  async function submit(text: string) {
    if (busy.value) return;
    const target = resumeTarget.value;
    pending.value = {
      path: `/conversations/${selectedId.value ? encodeURIComponent(selectedId.value) : "new"}/messages`,
      body: target ? { content_text: text, reply_to: { question_message_id: target.question_message_id, waiting_version: target.waiting_version } }
        : { content_text: text },
      key: crypto.randomUUID(), conversationId: selectedId.value,
      draftKey: draftKey.value,
      ...(selectedId.value ? {} : { createKey: crypto.randomUUID() }),
    };
    pending.value.body.attachments = draft.value.attachment ? [draft.value.attachment] : [];
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

  async function regenerate(message: ChatMessage) {
    if (busy.value || message.phase !== "answer") return;
    pending.value = { path: `/messages/${encodeURIComponent(message.message_id)}/regenerate`, body: {},
      key: crypto.randomUUID(), conversationId: selectedId.value };
    persistPending(); await sendPending();
  }

  async function retry(run: AgentRun, invocationId: string) {
    if (busy.value) return;
    pending.value = { path: `/agent-runs/${encodeURIComponent(run.agent_run_id)}/retry`,
      body: { retry_type: "TOOL_RETRY", invocation_run_id: invocationId }, key: crypto.randomUUID(), conversationId: run.conversation_id };
    persistPending(); await sendPending();
  }

  async function resume(run: AgentRun) {
    if (busy.value || run.status !== "INTERRUPTED" || !run.submission_id || (run.can_resume === false &&
        !(run.resume_after && ['CONTINUE', 'FIX_CONFIGURATION'].includes(run.recovery_action ?? '') && Date.now() >= Date.parse(run.resume_after)))) return;
    pending.value = { path: `/agent-runs/${encodeURIComponent(run.agent_run_id)}/resume`,
      body: { submission_id: run.submission_id, version: run.version }, key: crypto.randomUUID(), conversationId: run.conversation_id };
    persistPending(); await sendPending();
  }

  async function remove(id: string) {
    if (sending.value || pending.value || uploading.value) return;
    await conversationsApi.deleteConversation?.(id);
    for (const key of Object.keys(drafts.value)) if (key.startsWith(`${id}:`)) delete drafts.value[key];
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(drafts.value));
    if (selectedId.value === id) await select(null);
    await refreshConversations();
  }

  async function ensureConversation(key: string) {
    if (selectedId.value) return selectedId.value;
    const originalGeneration = generation;
    const oldDraft = { ...draft.value };
    const created = await conversationsApi.createConversation(undefined, key);
    if (generation === originalGeneration) {
      await select(created.data.conversation_id);
      if (generation === originalGeneration + 1) saveDraft(oldDraft);
    }
    await refreshConversations();
    return created.data.conversation_id;
  }

  async function deleted(id: string) {
    for (const key of Object.keys(drafts.value)) if (key.startsWith(`${id}:`)) delete drafts.value[key];
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(drafts.value));
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
  onUnmounted(() => { if (timer) clearInterval(timer); generation++; streams.reset(); });
  return { conversations, selectedId, runs, messages, loading, sending, generating, stopping, stop, regenerate, completed, busy, writeBlocked, error, pending, nextCursor, conversationCursor, ensureConversation, deleted,
    uploading, uploadError, canRetryUpload, draft, setDraftText, uploadAttachment, checkUpload, removeAttachment,
    processes: streams.processes, reconnecting: streams.reconnecting, resume,
    resumeTarget, initialize, select, refresh, refreshConversations, submit, sendPending, confirm, retry, remove };
}
