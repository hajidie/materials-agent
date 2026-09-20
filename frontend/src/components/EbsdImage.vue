<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import AssetGallery from "./AssetGallery.vue";
import { agentRequest } from "../api/agent";
import type { AssetSummary } from "../api/types";

const props = defineProps<{ assetId: string; preview?: boolean }>();
const image = ref<AssetSummary | null>(null);
const error = ref(false);
let generation = 0;
async function load() {
  const current = ++generation;
  image.value = null;
  error.value = false;
  try {
    const metadata = await agentRequest<AssetSummary>(`/assets/${encodeURIComponent(props.assetId)}`);
    if (current !== generation) return;
    if (metadata.status !== "AVAILABLE") throw new Error("Unavailable image");
    image.value = { ...metadata, content_url: `/api/v1/assets/${encodeURIComponent(props.assetId)}/content` };
  } catch { if (current === generation) error.value = true; }
}
watch(() => props.assetId, () => { void load(); }, { immediate: true });
onUnmounted(() => { generation++; });
</script>
<template>
  <section aria-label="EBSD 输入图片">
    <AssetGallery v-if="image" :assets="[image]" :preview="preview" image-label="用户上传的 EBSD 输入图片" />
    <p v-else-if="!error" role="status">正在加载 EBSD 图片…</p>
    <div v-else role="status"><p>EBSD 图片暂时无法加载。</p><button type="button" class="button" @click="load">重试加载图片</button></div>
  </section>
</template>
