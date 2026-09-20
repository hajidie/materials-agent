<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { agentRequest } from "../api/agent";
import type { ArtifactTarget, ArtifactView } from "../api/artifacts";
import ResultPresentation from "./ResultPresentation.vue";
const props = defineProps<{ target: ArtifactTarget }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement | null>(null), value = ref<ArtifactView | null>(null), error = ref(false), loading = ref(true);
const originalFocus = document.activeElement as HTMLElement | null;
const imageError = ref(false);
let controller: AbortController | null = null;
async function load() {
  controller?.abort(); controller = new AbortController();
  const signal = controller.signal;
  loading.value = true; error.value = false; imageError.value = false; value.value = null;
  const t = props.target;
  try {
    const result = await agentRequest<ArtifactView>(`/conversations/${encodeURIComponent(t.conversation)}/messages/${encodeURIComponent(t.message)}/artifacts/${encodeURIComponent(t.attachment.attachment_id)}`, { signal });
    if (!signal.aborted) value.value = result;
  } catch { if (!signal.aborted) error.value = true; }
  finally { if (!signal.aborted) loading.value = false; }
}
onMounted(async () => { await nextTick(); dialog.value?.showModal(); void load(); });
watch(() => props.target, () => { void load(); });
onUnmounted(() => { controller?.abort(); originalFocus?.focus(); });
</script>
<template>
  <dialog ref="dialog" class="artifact-viewer" aria-labelledby="artifact-title" @cancel.prevent="emit('close')" @click="event => { if (event.target === dialog) emit('close'); }">
    <header><h2 id="artifact-title">{{ target.attachment.name }}</h2><button class="button" autofocus aria-label="关闭详情" @click="emit('close')">关闭</button></header>
    <p v-if="loading" role="status">正在加载详情…</p>
    <section v-else-if="error" role="alert"><p>暂时无法读取详情。已发送的消息保持不变。</p><button class="button" @click="load">重新加载</button></section>
    <template v-else-if="value">
      <section v-if="imageError" role="alert"><p>图片暂时无法加载。</p><button class="button" @click="load">重新加载图片</button></section>
      <img v-else-if="value.image_url" :src="value.image_url" :alt="value.name" @error="imageError = true" />
      <ResultPresentation v-if="value.presentation" :value="value.presentation" details />
      <footer><a v-for="download in value.downloads" :key="download.url" class="button" :href="download.url" download>{{ download.name }}</a></footer>
    </template>
  </dialog>
</template>
