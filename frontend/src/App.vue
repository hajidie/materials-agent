<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import ConversationSidebar from "./components/ConversationSidebar.vue";
import ConversationView from "./components/ConversationView.vue";
import GlobalErrorNotice from "./components/GlobalErrorNotice.vue";
import DeleteConversationDialog from "./components/DeleteConversationDialog.vue";
import { toUserVisibleError } from "./api/errors";
import { useMaterialsAgent } from "./composables/useMaterialsAgent";

const agent = useMaterialsAgent();
const creatingConversation = ref(false);
const deleteCandidateId = ref<string | null>(null);
const deletePending = ref(false);
const deleteError = ref<string | null>(null);

const selectedConversation = computed(
  () =>
    agent.conversations.value.find(
      (conversation) =>
        conversation.conversation_id ===
        agent.selectedConversationId.value,
    ) ?? null,
);

const writeBusy = computed(
  () =>
    creatingConversation.value ||
    agent.mutationStatus.value === "SENDING" ||
    agent.mutationStatus.value === "UNCERTAIN",
);

const deleteCandidate = computed(() =>
  agent.conversations.value.find(
    (conversation) => conversation.conversation_id === deleteCandidateId.value,
  ) ?? null,
);

onMounted(() => {
  void (async () => {
    try {
      await agent.initialize();
    } catch {
      // useMaterialsAgent already owns the bounded user-visible error.
    }
    agent.startPolling();
  })();
});

async function createConversation(): Promise<void> {
  if (writeBusy.value) {
    return;
  }
  if (typeof agent.showBlankWorkspace === "function") {
    agent.showBlankWorkspace();
    return;
  }
  creatingConversation.value = true;
  try {
    await agent.createConversation();
  } finally {
    creatingConversation.value = false;
  }
}

function openDeleteDialog(conversationId: string): void {
  if (writeBusy.value) {
    return;
  }
  deleteCandidateId.value = conversationId;
  deleteError.value = null;
}

function closeDeleteDialog(): void {
  if (!deletePending.value) {
    deleteCandidateId.value = null;
    deleteError.value = null;
  }
}

async function confirmDeleteConversation(): Promise<void> {
  const conversationId = deleteCandidateId.value;
  if (conversationId === null || deletePending.value) {
    return;
  }
  deletePending.value = true;
  deleteError.value = null;
  try {
    await agent.deleteConversation(conversationId);
    deleteCandidateId.value = null;
  } catch (error) {
    deleteError.value = toUserVisibleError(error).message;
  } finally {
    deletePending.value = false;
  }
}

function ignoreRejected(operation: Promise<unknown>): void {
  void operation.catch(() => undefined);
}
</script>

<template>
  <div
    class="app-shell"
    :data-mutation-status="agent.mutationStatus.value"
    :inert="deleteCandidate !== null"
  >
    <ConversationSidebar
      :conversations="agent.conversations.value"
      :selected-conversation-id="agent.selectedConversationId.value"
      :loading="agent.conversationListLoading.value"
      :has-more="agent.conversationNextCursor.value !== null"
      :creating="creatingConversation"
      :create-disabled="writeBusy"
      @create="createConversation"
      @refresh="ignoreRejected(agent.refreshConversations())"
      @select="ignoreRejected(agent.selectConversation($event))"
      @delete="openDeleteDialog"
      @load-more="ignoreRejected(agent.loadMoreConversations())"
    />

    <section class="app-main">
      <GlobalErrorNotice
        :error="agent.globalError.value"
        :errors="agent.globalErrors.value"
        :pending-mutation="agent.pendingMutation.value"
        :mutation-status="agent.mutationStatus.value"
        @retry-pending="ignoreRejected(agent.retryPendingMutation())"
        @discard-pending="agent.discardPendingMutation()"
      />

      <ConversationView
        :selected-conversation="selectedConversation"
        :timeline="agent.timeline.value"
        :timeline-loading="agent.timelineLoading.value"
        :supplement-target="agent.supplementTarget.value"
        :mutation-status="agent.mutationStatus.value"
        :write-busy="writeBusy"
        :initial-draft="agent.firstTurnDraft?.value ?? ''"
        @submit-new-task="ignoreRejected(agent.submitNewTask($event))"
        @submit-supplement="ignoreRejected(agent.submitSupplement($event))"
        @set-supplement-target="agent.setSupplementTarget($event)"
        @cancel-supplement-target="agent.cancelSupplementTarget()"
        @retry-tool="ignoreRejected(agent.retryTool($event))"
        @retry-explanation="
          ignoreRejected(agent.retryExplanation($event))
        "
        @refresh="ignoreRejected(agent.refreshTimeline())"
      />
    </section>
  </div>

  <DeleteConversationDialog
    v-if="deleteCandidate"
    :title="deleteCandidate.title?.trim() || '新对话'"
    :pending="deletePending"
    :error="deleteError"
    @cancel="closeDeleteDialog"
    @confirm="confirmDeleteConversation"
  />
</template>
