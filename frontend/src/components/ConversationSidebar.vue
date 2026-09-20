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
  select: [conversationId: string];
  delete: [conversationId: string];
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
        <h1>材料智能助手</h1>
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
    </header>

    <p v-if="loading" class="loading-text" role="status">
      正在加载对话…
    </p>

    <nav class="conversation-list" aria-label="已保存对话">
      <p v-if="conversations.length === 0 && !loading" class="empty-state">
        还没有对话。新建一个对话开始材料研究。
      </p>
      <div
        v-for="conversation in conversations"
        :key="conversation.conversation_id"
        class="conversation-list__row"
        :class="{
          'conversation-list__item--selected':
            selectedConversationId === conversation.conversation_id,
        }"
      >
        <button
          type="button"
          class="conversation-list__item"
          :class="{
            'conversation-list__item--selected':
              selectedConversationId === conversation.conversation_id,
          }"
          :data-conversation-id="conversation.conversation_id"
          :title="displayTitle(conversation.title)"
          :aria-current="
            selectedConversationId === conversation.conversation_id
              ? 'true'
              : undefined
          "
          @click="$emit('select', conversation.conversation_id)"
        >
          <strong>{{ displayTitle(conversation.title) }}</strong>
        </button>
        <button
          type="button"
          class="conversation-list__delete"
          data-action="delete-conversation"
          :aria-label="`永久删除对话：${displayTitle(conversation.title)}`"
          :disabled="createDisabled"
          @click="$emit('delete', conversation.conversation_id)"
        >
          删除
        </button>
      </div>
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
