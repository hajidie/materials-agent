<script setup lang="ts">
import { ref, watch } from "vue";
import EbsdImage from "./EbsdImage.vue";
const props = defineProps<{ disabled: boolean; sending: boolean; completed: number; waitingQuestion: string | null; initialDraft?: string; ebsdAssetId?: string | undefined; uploading?: boolean; uploadError?: string | null; canRetryUpload?: boolean }>();
const emit = defineEmits<{ submit: [text: string]; "cancel-resume": []; "update-draft": [text: string]; "upload-ebsd": [file: File]; "remove-ebsd": []; "retry-ebsd": [] }>();
const draft = ref(props.initialDraft ?? "");
const invalid = ref(false);
watch(draft, value => emit("update-draft", value));
function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const selected = input.files?.[0];
  if (selected) { emit("upload-ebsd", selected); }
  input.value = "";
}
watch(() => props.completed, () => { draft.value = ""; invalid.value = false; });
watch(() => props.initialDraft, value => { if (value && !draft.value) draft.value = value; });
function submit() {
  if (props.disabled || props.sending) return;
  invalid.value = !draft.value.trim() && !props.ebsdAssetId;
  if (!invalid.value) emit("submit", draft.value.trim() || "预测这张 Inconel 625 EBSD 图片的屈服强度。");
}
function onKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); }
}
</script>
<template>
  <section class="composer" aria-label="发送消息">
    <div v-if="waitingQuestion" class="supplement-banner"><p>正在补充：{{ waitingQuestion }}</p><button class="button button--text" :disabled="disabled" @click="$emit('cancel-resume')">改为新目标</button></div>
    <form class="composer__form" novalidate @submit.prevent="submit">
      <label for="materialsagent-message">{{ waitingQuestion ? '补充信息' : '研究目标' }}</label>
      <textarea class="resize-none" id="materialsagent-message" v-model="draft" rows="4" maxlength="32768" :disabled="disabled"
        :aria-invalid="invalid" :aria-describedby="invalid ? 'composer-error' : 'composer-hint'" @keydown="onKeydown" />
      <section class="ebsd-upload" aria-label="上传 EBSD 图片">
        <label for="ebsd-image-file">上传 EBSD 图片</label>
        <input id="ebsd-image-file" type="file" aria-describedby="ebsd-upload-hint" accept="image/png,image/jpeg" :disabled="disabled" @change="chooseFile" />
        <p id="ebsd-upload-hint" class="composer__hint">Inconel 625 · 单张正方形 RGB PNG/JPEG · 128–4096 像素 · 最大 10 MiB</p>
        <p v-if="uploading" role="status">正在上传 EBSD 图片…</p>
        <p v-if="uploadError" class="field-error" role="alert">{{ uploadError }}</p>
        <button v-if="uploadError && canRetryUpload" class="button" type="button" :disabled="disabled" @click="$emit('retry-ebsd')">重试上传</button>
        <template v-if="ebsdAssetId">
          <EbsdImage :asset-id="ebsdAssetId" />
          <button class="button" type="button" :disabled="disabled" @click="$emit('remove-ebsd')">移除 EBSD 图片</button>
        </template>
      </section>
      <p id="composer-hint" class="composer__hint">Enter 发送，Shift+Enter 换行</p>
      <p v-if="invalid" id="composer-error" class="field-error" role="alert">请输入消息后再发送。</p>
      <button class="button button--primary" type="submit" :disabled="disabled" :aria-busy="sending">{{ sending ? '正在执行…' : waitingQuestion ? '补充并继续' : '发送目标' }}</button>
    </form>
  </section>
</template>
