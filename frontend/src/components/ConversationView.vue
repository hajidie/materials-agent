<script setup lang="ts">
import { computed, type DeepReadonly } from "vue";

import type {
  ConversationListItem,
  TimelineItem,
} from "../api/types";
import type { MutationStatus } from "../composables/useIdempotentRequest";
import type { SupplementTarget } from "../composables/useMaterialsAgent";
import ChatComposer from "./ChatComposer.vue";
import TimelineList from "./TimelineList.vue";

defineOptions({ name: "ConversationView" });

const props = defineProps<{
  selectedConversation:
    | ConversationListItem
    | DeepReadonly<ConversationListItem>
    | null;
  timeline: readonly (TimelineItem | DeepReadonly<TimelineItem>)[];
  timelineLoading: boolean;
  supplementTarget:
    | SupplementTarget
    | DeepReadonly<SupplementTarget>
    | null;
  mutationStatus: MutationStatus;
  writeBusy: boolean;
}>();

defineEmits<{
  "submit-new-task": [contentText: string];
  "submit-supplement": [contentText: string];
  "set-supplement-target": [target: SupplementTarget];
  "cancel-supplement-target": [];
  "retry-tool": [taskId: string];
  "retry-explanation": [resultId: string];
  refresh: [];
}>();

const title = computed(() => {
  const value = props.selectedConversation?.title?.trim();
  return value ? value : "新对话";
});
</script>

<template>
  <main class="conversation-view">
    <section
      v-if="!selectedConversation"
      class="conversation-empty"
      aria-labelledby="conversation-guide-title"
    >
      <p class="eyebrow">开始研究</p>
      <h2 id="conversation-guide-title">请选择或新建一个对话</h2>
      <p>
        你可以提出材料知识问题，也可以描述完整的 ZTA35G 工艺与期望输出。
      </p>
    </section>

    <template v-else>
      <header class="conversation-view__header">
        <div>
          <p class="eyebrow">当前对话</p>
          <h2>{{ title }}</h2>
        </div>
        <button
          type="button"
          class="button button--secondary"
          data-action="refresh-timeline"
          :disabled="timelineLoading"
          @click="$emit('refresh')"
        >
          {{ timelineLoading ? "刷新中…" : "刷新" }}
        </button>
      </header>

      <div class="conversation-view__timeline">
        <p v-if="timelineLoading" class="loading-text" role="status">
          正在读取最新持久化事实…
        </p>
        <TimelineList
          :items="timeline"
          :conversation-id="selectedConversation.conversation_id"
          :mutation-busy="writeBusy"
          @set-supplement-target="$emit('set-supplement-target', $event)"
          @retry-tool="$emit('retry-tool', $event)"
          @retry-explanation="$emit('retry-explanation', $event)"
        />
      </div>

      <ChatComposer
        :key="selectedConversation.conversation_id"
        :disabled="writeBusy"
        :sending="mutationStatus === 'SENDING'"
        :supplement-target="supplementTarget"
        :mutation-status="mutationStatus"
        @submit-new-task="$emit('submit-new-task', $event)"
        @submit-supplement="$emit('submit-supplement', $event)"
        @cancel-supplement="$emit('cancel-supplement-target')"
      />
    </template>
  </main>
</template>
