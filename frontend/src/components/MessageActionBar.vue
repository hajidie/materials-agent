<script setup lang="ts">
import { onUnmounted, ref } from "vue";
defineProps<{ text: string; canRegenerate: boolean; disabled: boolean }>();
defineEmits<{ regenerate: [] }>();
const feedback = ref("");
let timer: ReturnType<typeof setTimeout> | undefined;
onUnmounted(() => { if (timer) clearTimeout(timer); });
async function copy(text: string) {
  try { await navigator.clipboard.writeText(text); feedback.value = "已复制"; }
  catch { feedback.value = "复制未完成，请手动复制"; }
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => { feedback.value = ""; }, 2000);
}
</script>
<template>
  <div class="message-actions" aria-label="回复操作">
    <button class="message-action" type="button" aria-label="复制回答" @click="copy(text)">
      <svg aria-hidden="true" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" /></svg>
      <span class="message-action__tooltip" role="tooltip">复制</span>
    </button>
    <button v-if="canRegenerate" class="message-action" type="button" aria-label="重新生成回答" :disabled="disabled" @click="$emit('regenerate')">
      <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 3M18 17a7 7 0 0 1-12 1l-2-3" /></svg>
      <span class="message-action__tooltip" role="tooltip">重新生成</span>
    </button>
    <span v-if="feedback" class="message-actions__feedback" role="status">{{ feedback }}</span>
  </div>
</template>
