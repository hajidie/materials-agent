<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import ConversationSidebar from "./components/ConversationSidebar.vue";
import DeleteConversationDialog from "./components/DeleteConversationDialog.vue";
import AgentRunCard from "./components/AgentRunCard.vue";
import ChatComposer from "./components/ChatComposer.vue";
import { clarificationLabel } from "./api/agent";
import { useAgentRuns } from "./composables/useAgentRuns";

const agent = useAgentRuns();
const pendingHere = computed(() => agent.pending.value?.conversationId === agent.selectedId.value ? agent.pending.value : null);
const composerText = computed(() => typeof pendingHere.value?.body.content_text === "string" ? pendingHere.value.body.content_text : agent.draft.value.text);
const composerAsset = computed(() => typeof pendingHere.value?.body.ebsd_asset_id === "string" ? pendingHere.value.body.ebsd_asset_id : agent.draft.value.assetId);
const deleteId = ref<string | null>(null);
const deleting = ref(false);
const deleteError = ref<string | null>(null);
const candidate = computed(() => agent.conversations.value.find(c => c.conversation_id === deleteId.value));
const selected = computed(() => agent.conversations.value.find(c => c.conversation_id === agent.selectedId.value));
onMounted(() => { void agent.initialize(); });
async function submit(text: string) {
  await agent.submit(text);
}
async function remove() {
  if (!deleteId.value || deleting.value) return;
  deleting.value = true;
  deleteError.value = null;
  try { await agent.remove(deleteId.value); deleteId.value = null; }
  catch { deleteError.value = "无法删除。运行中的对话需要等待执行结束，请刷新后重试。"; }
  finally { deleting.value = false; }
}
function safely(promise: Promise<unknown>) { void promise.catch(() => { agent.error.value = "操作未完成，请刷新后重试。"; }); }
</script>

<template>
  <div class="app-shell" :inert="candidate !== undefined">
    <ConversationSidebar :conversations="agent.conversations.value" :selected-conversation-id="agent.selectedId.value"
      :loading="agent.loading.value" :has-more="agent.conversationCursor.value !== null" :creating="false" :create-disabled="agent.busy.value"
      @create="safely(agent.select(null))" @select="safely(agent.select($event))" @delete="deleteId = $event"
      @refresh="safely(agent.refreshConversations())" @load-more="safely(agent.refreshConversations(true))" />
    <main class="app-main">
      <header class="conversation-header"><h2>{{ selected?.title || '材料研究' }}</h2><button class="button" @click="safely(agent.refresh())">刷新运行状态</button></header>
      <section v-if="agent.error.value || agent.pending.value" class="global-error" role="status">
        <p>{{ agent.error.value || (agent.sending.value ? '请求正在执行，进度会自动更新。' : '有一项提交尚未确认结果。') }}</p>
        <button v-if="agent.pending.value" class="button" :disabled="agent.sending.value" @click="safely(agent.sendPending())">检查原提交</button>
      </section>
      <p v-if="agent.loading.value" role="status">正在加载运行记录…</p>
      <p v-else-if="agent.runs.value.length === 0" class="empty-state">描述你的研究目标，或输入“1000 MPa 转 GPa”开始。</p>
      <button v-if="agent.nextCursor.value" class="button" @click="safely(agent.refresh(true))">加载更早的运行</button>
      <section class="agent-timeline" aria-label="研究运行记录">
        <AgentRunCard v-for="run in agent.runs.value" :key="run.agent_run_id" :run="run" :disabled="agent.busy.value"
          @resume="agent.resumeTarget.value = run" @confirm="safely(agent.confirm(run, $event))"
          @regenerate="safely(agent.retry(run))" @retry="safely(agent.retry(run, $event))" />
      </section>
      <ChatComposer :key="`${agent.selectedId.value}:${agent.resumeTarget.value?.agent_run_id ?? 'new'}`" :disabled="agent.busy.value" :sending="agent.sending.value" :completed="agent.completed.value"
        :waiting-question="agent.resumeTarget.value?.waiting ? clarificationLabel(agent.resumeTarget.value.waiting.question) : null"
        :initial-draft="composerText"
        :ebsd-asset-id="composerAsset" :uploading="agent.uploading.value" :upload-error="agent.uploadError.value" :can-retry-upload="agent.canRetryUpload.value"
        @update-draft="agent.setDraftText($event)" @upload-ebsd="safely(agent.uploadEbsd($event))" @remove-ebsd="agent.removeEbsd()" @retry-ebsd="safely(agent.retryEbsdUpload())"
        @submit="safely(submit($event))" @cancel-resume="agent.resumeTarget.value = null" />
    </main>
  </div>
  <DeleteConversationDialog v-if="candidate" :title="candidate.title || '新对话'" :pending="deleting" :error="deleteError"
    @cancel="deleteId = null" @confirm="safely(remove())" />
</template>
