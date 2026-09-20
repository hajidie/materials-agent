<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import ConversationSidebar from "./components/ConversationSidebar.vue";
import DeleteConversationDialog from "./components/DeleteConversationDialog.vue";
import AgentRunCard from "./components/AgentRunCard.vue";
import ChatComposer from "./components/ChatComposer.vue";
import { agentRequest, clarificationLabel } from "./api/agent";
import { useAgentRuns } from "./composables/useAgentRuns";
import { useChatArtifacts } from "./composables/useChatArtifacts";
import ArtifactViewer from "./components/ArtifactViewer.vue";
import MessageAttachment from "./components/MessageAttachment.vue";
import type { ArtifactTarget, Attachment } from "./api/artifacts";
import { ApiResponseError } from "./api/errors";
import type { AgentRun } from "./api/agent";

const agent = useAgentRuns();
const chat = useChatArtifacts(agent.selectedId, agent.runs);
const viewing = ref<ArtifactTarget | null>(null);
const reconciling = ref<string | null>(null), receipts = ref<Record<string, string>>({});
const deleteOperation = ref<string | null>(null);
watch(chat.fence, value => { agent.writeBlocked.value = !!value; });
watch(agent.selectedId, () => { viewing.value = null; });
const pendingHere = computed(() => agent.pending.value?.conversationId === agent.selectedId.value ? agent.pending.value : null);
const composerText = computed(() => typeof pendingHere.value?.body.content_text === "string" ? pendingHere.value.body.content_text : agent.draft.value.text);
const composerAttachment = computed(() => Array.isArray(pendingHere.value?.body.attachments) ? pendingHere.value.body.attachments[0] as Attachment | undefined : agent.draft.value.attachment);
const timeline = computed(() => [...agent.runs.value.map(run => ({ key: run.agent_run_id, created: run.created_at, run, result: null })), ...chat.messages.value.map(result => ({ key: result.message_id, created: result.created_at, result, run: null }))].sort((a, b) => a.created.localeCompare(b.created) || a.key.localeCompare(b.key)));
const deleteId = ref<string | null>(null);
const deleting = ref(false);
const deleteError = ref<string | null>(null);
const candidate = computed(() => agent.conversations.value.find(c => c.conversation_id === deleteId.value));
onMounted(() => { void agent.initialize(); });
async function submit(text: string) {
  await agent.submit(text);
}
async function remove() {
  if (!deleteId.value || deleting.value) return;
  deleting.value = true;
  deleteError.value = null;
  try { await agent.remove(deleteId.value); deleteId.value = null; }
  catch (cause) {
    if (cause instanceof ApiResponseError && cause.code === "CONVERSATION_DELETE_PENDING") {
      deleteOperation.value = cause.details.find(d => d.operation_id)?.operation_id ?? null;
      deleteError.value = "删除结果待核查。对话和新工作禁令将保留，直到服务端确认结果。";
    } else if (cause instanceof ApiResponseError && cause.code === "CONVERSATION_BUSY") {
      deleteError.value = "仍有活动或未确认操作，暂时不能删除。任务不会被自动取消。";
    } else deleteError.value = "无法确认删除结果。请核查当前对话，避免重复操作。";
    await chat.refreshState();
    deleteOperation.value ??= agent.selectedId.value === deleteId.value ? chat.fence.value : null;
  }
  finally { deleting.value = false; }
}
watch(deleteId, id => { deleteError.value = null; deleteOperation.value = id === agent.selectedId.value ? chat.fence.value : null; });
async function reconcileDelete() {
  const operation = deleteOperation.value ?? chat.fence.value;
  const id = deleteId.value ?? agent.selectedId.value;
  if (!operation || !id || deleting.value) return;
  deleting.value = true;
  try {
    const result = await agentRequest<{ status: string }>(`/conversation-deletions/${encodeURIComponent(operation)}/reconcile`, { body: {} });
    if (result.status === "COMPLETED") { await agent.deleted(id); deleteId.value = null; deleteOperation.value = null; }
    else { deleteError.value = result.status === "REJECTED_BUSY" ? "活动工作尚未结束，本次删除已拒绝；不会自动取消任务。" : "删除仍待核查，对话继续保留。"; await chat.refreshState(); }
  } catch (cause) { deleteError.value = "暂时无法确认删除结果，请稍后核查。"; }
  finally { deleting.value = false; }
}
async function reconcileRun(run: AgentRun, invocation: string) {
  if (reconciling.value) return;
  reconciling.value = run.agent_run_id;
  try {
    await agentRequest(`/agent-runs/${encodeURIComponent(run.agent_run_id)}/invocations/${encodeURIComponent(invocation)}/reconcile`, { body: {} });
    receipts.value[run.agent_run_id] = "已核查原操作，历史回复保持不变。已确认的完成结果会追加到对话。";
    if (agent.selectedId.value === run.conversation_id) await chat.observe(true);
  } catch { receipts.value[run.agent_run_id] = "暂时无法确认结果，请稍后核查。"; }
  finally { reconciling.value = null; }
}
function view(value: { message: string; attachment: Attachment }) {
  if (agent.selectedId.value) viewing.value = { conversation: agent.selectedId.value, ...value };
}
function safely(promise: Promise<unknown>) { void promise.catch(() => { agent.error.value = "操作未完成，请刷新后重试。"; }); }
</script>

<template>
  <div class="app-shell" :inert="candidate !== undefined">
    <ConversationSidebar :conversations="agent.conversations.value" :selected-conversation-id="agent.selectedId.value"
      :loading="agent.loading.value" :has-more="agent.conversationCursor.value !== null" :creating="false" :create-disabled="agent.busy.value"
      @create="safely(agent.select(null))" @select="safely(agent.select($event))" @delete="deleteId = $event"
      @load-more="safely(agent.refreshConversations(true))" />
    <main class="app-main" :class="{ 'app-main--empty': agent.runs.value.length === 0 && !agent.loading.value }">
      <section class="conversation-scroll">
      <section v-if="chat.fence.value" class="global-error" role="status"><p>对话删除待核查，新工作已暂停。关闭提示或重新打开页面不会解除禁令。</p><p v-if="deleteError">{{ deleteError }}</p><button class="button" :disabled="deleting" @click="safely(reconcileDelete())">核查对话删除</button></section>
      <section v-if="agent.error.value || (agent.pending.value && !agent.sending.value)" class="global-error" role="status">
        <p>{{ agent.error.value || '有一项提交尚未确认结果。' }}</p>
        <button v-if="agent.pending.value && !agent.sending.value" class="button" :disabled="!!chat.fence.value" @click="safely(agent.sendPending())">检查原提交</button>
      </section>
      <p v-if="agent.loading.value" role="status">正在加载对话…</p>
      <button v-if="agent.nextCursor.value" class="button" @click="safely(agent.refresh(true))">加载更早的消息</button>
      <section v-if="timeline.length" class="agent-timeline" aria-label="对话消息">
        <template v-for="item in timeline" :key="item.key">
          <AgentRunCard v-if="item.run" :run="item.run" :disabled="agent.busy.value" :reconciling="reconciling !== null" :receipt="receipts[item.run.agent_run_id]"
            @resume="agent.resumeTarget.value = item.run" @confirm="safely(agent.confirm(item.run, $event))"
            @regenerate="safely(agent.retry(item.run))" @retry="safely(agent.retry(item.run, $event))"
            @artifact="view($event)" @reconcile="safely(reconcileRun(item.run, $event))" />
          <article v-else-if="item.result" class="chat-assistant chat-result" aria-label="完成结果"><p class="agent-answer">{{ item.result.text }}</p>
            <MessageAttachment v-for="attachment in item.result.artifacts" :key="attachment.attachment_id"
              :conversation-id="agent.selectedId.value!" :message-id="item.result.message_id" :attachment="attachment"
              @open="view({ message: item.result.message_id, attachment })" />
          </article>
        </template>
      </section>
      <section v-else-if="!agent.loading.value" class="chat-welcome"><h1>今天想研究什么？</h1><p>描述你的问题，或添加实验数据和 EBSD 图片。</p></section>
      <p v-if="chat.pending.value" class="muted" role="status">正在处理已提交的数据，完成后会在这里显示结果。</p>
      <section v-if="chat.notice.value" role="status"><p>{{ chat.notice.value }}</p><button class="button" :disabled="!!chat.fence.value" @click="safely(chat.observe(true))">核查结果</button></section>
      </section>
      <ChatComposer :key="`${agent.selectedId.value}:${agent.resumeTarget.value?.agent_run_id ?? 'new'}`" :disabled="agent.busy.value" :sending="agent.sending.value" :completed="agent.completed.value"
        :waiting-question="agent.resumeTarget.value?.waiting ? clarificationLabel(agent.resumeTarget.value.waiting.question) : null"
        :initial-draft="composerText"
        :attachment="composerAttachment" :uploading="agent.uploading.value" :upload-error="agent.uploadError.value" :can-retry-upload="agent.canRetryUpload.value"
        @update-draft="agent.setDraftText($event)" @upload="safely(agent.uploadAttachment($event))" @remove-attachment="agent.removeAttachment()" @check-upload="safely(agent.checkUpload())"
        @submit="safely(submit($event))" @cancel-resume="agent.resumeTarget.value = null" />
    </main>

  </div>
  <ArtifactViewer v-if="viewing" :target="viewing" @close="viewing = null" />
  <DeleteConversationDialog v-if="candidate" :title="candidate.title || '新对话'" :pending="deleting" :error="deleteError" :reconciling="!!deleteOperation"
    @cancel="deleteId = null" @confirm="safely(deleteOperation ? reconcileDelete() : remove())" />
</template>
