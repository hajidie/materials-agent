<script setup lang="ts">
import type { DeepReadonly } from "vue";

import type { ConversationListItem } from "../api/types";

defineOptions({ name: "ConversationSidebar" });

defineProps<{
  conversations: readonly (
    | ConversationListItem
    | DeepReadonly<ConversationListItem>
  )[];
  selectedConversationId: string | null;
  loading: boolean;
  hasMore: boolean;
  creating: boolean;
  createDisabled?: boolean;
}>();

defineEmits<{
  create: [];
  refresh: [];
  select: [conversationId: string];
  "load-more": [];
}>();

function displayTitle(title: string | null): string {
  const value = title?.trim();
  return value ? value : "新对话";
}
</script>

<template>
  <aside class="conversation-sidebar" aria-label="对话列表">
    <header class="conversation-sidebar__header">
      <div>
        <p class="eyebrow">本地材料研究平台</p>
        <h1>材料智能体</h1>
      </div>
      <button
        type="button"
        class="button button--primary button--full"
        data-action="create-conversation"
        :disabled="creating || createDisabled"
        @click="$emit('create')"
      >
        {{ creating ? "创建中…" : "新建对话" }}
      </button>
      <button
        type="button"
        class="button button--secondary button--full"
        data-action="refresh-conversations"
        aria-label="刷新对话列表"
        :disabled="loading"
        @click="$emit('refresh')"
      >
        {{ loading ? "刷新中…" : "刷新对话列表" }}
      </button>
    </header>

    <p v-if="loading" class="loading-text" role="status">
      正在加载对话…
    </p>

    <nav class="conversation-list" aria-label="已保存对话">
      <p v-if="conversations.length === 0 && !loading" class="empty-state">
        还没有对话。新建一个对话开始材料研究。
      </p>
      <button
        v-for="conversation in conversations"
        :key="conversation.conversation_id"
        type="button"
        class="conversation-list__item"
        :class="{
          'conversation-list__item--selected':
            selectedConversationId === conversation.conversation_id,
        }"
        :data-conversation-id="conversation.conversation_id"
        :aria-current="
          selectedConversationId === conversation.conversation_id
            ? 'true'
            : undefined
        "
        @click="$emit('select', conversation.conversation_id)"
      >
        <strong>{{ displayTitle(conversation.title) }}</strong>
        <span>
          {{ conversation.last_activity_preview || "暂无消息" }}
        </span>
      </button>
    </nav>

    <button
      v-if="hasMore"
      type="button"
      class="button button--secondary button--full"
      data-action="load-more"
      :disabled="loading"
      @click="$emit('load-more')"
    >
      {{ loading ? "加载中…" : "加载更多" }}
    </button>
  </aside>
</template>
