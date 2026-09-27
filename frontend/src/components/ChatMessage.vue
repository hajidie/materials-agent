<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { agentRequest, type ChatMessage } from "../api/agent";
import type { Attachment } from "../api/artifacts";
import MessageAttachment from "./MessageAttachment.vue";
import MessageActionBar from "./MessageActionBar.vue";
const props = defineProps<{ message: ChatMessage; conversationId: string; disabled: boolean }>();
defineEmits<{ regenerate: [message: ChatMessage]; artifact: [value: { message: string; attachment: Attachment }] }>();
const selected = ref<ChatMessage | null>(null), loading = ref(false), error = ref("");
const versions = ref<ChatMessage[]>([]);
const displayed = computed(() => selected.value ?? props.message);
watch(() => props.message.message_id, () => { selected.value = null; versions.value = []; });
async function switchVersion(delta: number) {
  if (loading.value) return;
  const identity = props.message.message_id;
  const wanted = (displayed.value.answer_version ?? 1) + delta;
  loading.value = true; error.value = "";
  try {
    const cached = versions.value.find(item => item.answer_version === wanted);
    if (cached) selected.value = cached;
    else {
      const result = await agentRequest<{ items: ChatMessage[] }>(`/messages/${encodeURIComponent(props.message.answer_root_message_id!)}/versions?before=${wanted + 1}&limit=1`);
      if (identity !== props.message.message_id) return;
      const found = result.items.find(item => item.answer_version === wanted);
      if (found) { versions.value.push(found); selected.value = found; }
    }
  } catch { error.value = "暂时无法加载这个版本。"; }
  finally { loading.value = false; }
}
</script>
<template>
  <article :class="message.role === 'USER' ? 'chat-user' : 'chat-assistant'" :aria-label="message.role === 'USER' ? '你的消息' : '助手回复'">
    <p class="agent-answer">{{ displayed.text }}</p>
    <div class="message-attachments">
      <MessageAttachment v-for="attachment in [...displayed.attachments, ...displayed.artifacts]" :key="attachment.attachment_id"
        :conversation-id="conversationId" :message-id="displayed.message_id" :attachment="attachment"
        @open="$emit('artifact', { message: displayed.message_id, attachment })" />
    </div>
    <div v-if="message.role === 'ASSISTANT'" class="message-footer">
      <MessageActionBar :text="displayed.text" :can-regenerate="displayed.phase === 'answer'" :disabled="disabled"
        @regenerate="$emit('regenerate', displayed)" />
      <nav v-if="message.version_count > 1" class="message-versions" aria-label="回答版本">
        <button type="button" aria-label="上一个回答版本" :disabled="loading || displayed.answer_version === 1" @click="switchVersion(-1)">‹</button>
        <span>{{ displayed.answer_version }} / {{ message.version_count }}</span>
        <button type="button" aria-label="下一个回答版本" :disabled="loading || displayed.answer_version === message.version_count" @click="switchVersion(1)">›</button>
      </nav>
    </div>
    <p v-if="error" class="field-error" role="status">{{ error }}</p>
  </article>
</template>
