<script setup lang="ts">
import type { DeepReadonly } from "vue";

import type { UserMessage } from "../api/types";

defineOptions({ name: "UserMessageItem" });

defineProps<{
  message: UserMessage | DeepReadonly<UserMessage>;
}>();

function formatTime(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return value;
  }
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(timestamp));
  } catch {
    return value;
  }
}
</script>

<template>
  <article class="message message--user" aria-label="用户消息">
    <p class="message__content">{{ message.content_text }}</p>
    <time
      v-if="message.created_at"
      class="message__time"
      :datetime="message.created_at"
    >
      {{ formatTime(message.created_at) }}
    </time>
  </article>
</template>
