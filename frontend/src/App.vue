<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import ConversationSidebar from "./components/ConversationSidebar.vue";
import ConversationView from "./components/ConversationView.vue";
import GlobalErrorNotice from "./components/GlobalErrorNotice.vue";
import { useMaterialsAgent } from "./composables/useMaterialsAgent";

const agent = useMaterialsAgent();
const creatingConversation = ref(false);

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
  if (
    writeBusy.value ||
    agent.conversationCreationUncertain.value
  ) {
    return;
  }
  creatingConversation.value = true;
  try {
    await agent.createConversation();
  } catch {
    // The composable owns safe error and uncertainty projection.
  } finally {
    creatingConversation.value = false;
  }
}

function ignoreRejected(operation: Promise<unknown>): void {
  void operation.catch(() => undefined);
}
</script>

<template>
  <div class="app-shell" :data-mutation-status="agent.mutationStatus.value">
    <ConversationSidebar
      :conversations="agent.conversations.value"
      :selected-conversation-id="agent.selectedConversationId.value"
      :loading="agent.conversationListLoading.value"
      :has-more="agent.conversationNextCursor.value !== null"
      :creating="creatingConversation"
      :create-disabled="
        writeBusy || agent.conversationCreationUncertain.value
      "
      @create="createConversation"
      @refresh="ignoreRejected(agent.refreshConversations())"
      @select="ignoreRejected(agent.selectConversation($event))"
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
        :task-details-by-id="agent.taskDetailsById.value"
        :task-details-loading-by-id="agent.taskDetailsLoadingById.value"
        @submit-new-task="ignoreRejected(agent.submitNewTask($event))"
        @submit-supplement="ignoreRejected(agent.submitSupplement($event))"
        @set-supplement-target="agent.setSupplementTarget($event)"
        @cancel-supplement-target="agent.cancelSupplementTarget()"
        @retry-tool="ignoreRejected(agent.retryTool($event))"
        @retry-explanation="
          ignoreRejected(agent.retryExplanation($event))
        "
        @load-task-history="
          ignoreRejected(agent.loadTaskHistory($event))
        "
        @refresh="ignoreRejected(agent.refreshTimeline())"
      />
    </section>
  </div>
</template>
