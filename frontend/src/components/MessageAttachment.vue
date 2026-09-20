<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";

import { agentRequest } from "../api/agent";
import type { Attachment, ArtifactView } from "../api/artifacts";

const props = defineProps<{
  conversationId: string;
  messageId: string;
  attachment: Attachment;
}>();

defineEmits<{ open: [] }>();

const isImage = computed(() => ["image", "ebsd_image"].includes(props.attachment.kind));
const imageUrl = ref<string | null>(null);
const loading = ref(false);
const error = ref(false);
let controller: AbortController | null = null;

async function loadImage() {
  if (!isImage.value) return;
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  loading.value = true;
  error.value = false;
  imageUrl.value = null;
  try {
    const value = await agentRequest<ArtifactView>(
      `/conversations/${encodeURIComponent(props.conversationId)}/messages/${encodeURIComponent(props.messageId)}/artifacts/${encodeURIComponent(props.attachment.attachment_id)}`,
      { signal },
    );
    if (!signal.aborted && value.image_url) imageUrl.value = value.image_url;
    else if (!signal.aborted) error.value = true;
  } catch {
    if (!signal.aborted) error.value = true;
  } finally {
    if (!signal.aborted) loading.value = false;
  }
}

onMounted(() => { void loadImage(); });
onUnmounted(() => controller?.abort());
</script>

<template>
  <button v-if="!isImage" type="button" class="attachment-card" @click="$emit('open')">
    {{ attachment.name }} <span>查看详情</span>
  </button>
  <figure v-else class="image-attachment">
    <button v-if="imageUrl" type="button" class="image-preview" :aria-label="`放大查看${attachment.name}`" @click="$emit('open')">
      <img :src="imageUrl" :alt="attachment.name" @error="error = true; imageUrl = null" />
    </button>
    <div v-else-if="loading" class="image-preview image-preview--placeholder" role="status">正在加载图片…</div>
    <div v-else class="image-preview image-preview--placeholder" role="alert">
      <span>图片暂时无法加载。</span>
      <button type="button" class="button button--text" @click="loadImage">重新加载</button>
    </div>
    <figcaption>{{ attachment.name }}<span v-if="imageUrl">点击图片放大</span></figcaption>
  </figure>
</template>
