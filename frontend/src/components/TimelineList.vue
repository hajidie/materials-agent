<script setup lang="ts">
import type { DeepReadonly } from "vue";

import type { TimelineItem } from "../api/types";
import AssistantMessageItem from "./AssistantMessageItem.vue";
import ToolTaskCard from "./ToolTaskCard.vue";
import ToolInvocationCard from "./ToolInvocationCard.vue";
import UserMessageItem from "./UserMessageItem.vue";

defineOptions({ name: "TimelineList" });

defineProps<{
  items: readonly (TimelineItem | DeepReadonly<TimelineItem>)[];
  conversationId: string;
  mutationBusy: boolean;
}>();

defineEmits<{
  "set-supplement-target": [
    target: {
      conversationId: string;
      taskId: string;
      summary: string;
    },
  ];
  "retry-tool": [taskId: string];
  "retry-explanation": [resultId: string];
  "confirm-invocation": [invocationRunId: string];
  "reject-invocation": [invocationRunId: string];
}>();
</script>

<template>
  <section class="timeline" aria-label="对话时间线">
    <p v-if="items.length === 0" class="empty-state">
      当前对话还没有消息。
    </p>

    <template v-for="item in items" :key="`${item.item_type}:${item.item_id}`">
      <div
        v-if="item.item_type === 'USER_MESSAGE'"
        :data-timeline-item="`${item.item_type}:${item.item_id}`"
      >
        <UserMessageItem :message="item.message" />
      </div>
      <div
        v-else-if="item.item_type === 'ASSISTANT_MESSAGE'"
        :data-timeline-item="`${item.item_type}:${item.item_id}`"
      >
        <AssistantMessageItem :message="item.message" />
      </div>
      <div
        v-else-if="item.item_type === 'TOOL_INVOCATION'"
        :data-timeline-item="`${item.item_type}:${item.item_id}`"
      >
        <ToolInvocationCard
          :invocation="item.invocation"
          :mutation-busy="mutationBusy"
          @confirm="$emit('confirm-invocation', $event)"
          @reject="$emit('reject-invocation', $event)"
        />
      </div>
      <div
        v-else
        :data-timeline-item="`${item.item_type}:${item.item_id}`"
      >
        <ToolTaskCard
          :item="item"
          :conversation-id="conversationId"
          :mutation-busy="mutationBusy"
          @supplement="$emit('set-supplement-target', $event)"
          @retry-tool="$emit('retry-tool', $event)"
          @retry-explanation="$emit('retry-explanation', $event)"
          @confirm-invocation="$emit('confirm-invocation', $event)"
          @reject-invocation="$emit('reject-invocation', $event)"
        />
      </div>
    </template>
  </section>
</template>
