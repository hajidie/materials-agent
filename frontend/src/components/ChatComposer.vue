<script setup lang="ts">
import { ref, watch } from "vue";
import type { Attachment } from "../api/artifacts";
const props = defineProps<{ disabled: boolean; sending: boolean; completed: number; waitingQuestion: string | null; initialDraft?: string; attachment?: Attachment | undefined; uploading?: boolean; uploadError?: string | null; canRetryUpload?: boolean }>();
const emit = defineEmits<{ submit: [text: string]; "cancel-resume": []; "update-draft": [text: string]; "upload": [file: File]; "remove-attachment": []; "check-upload": [] }>();
const draft = ref(props.initialDraft ?? "");
const invalid = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
watch(draft, value => emit("update-draft", value));
function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const selected = input.files?.[0];
  if (selected) { emit("upload", selected); }
  input.value = "";
}
watch(() => props.completed, () => { draft.value = ""; invalid.value = false; });
watch(() => props.initialDraft, value => { if (value && !draft.value) draft.value = value; });
function submit() {
  if (props.disabled || props.sending) return;
  invalid.value = !draft.value.trim() && !props.attachment;
  if (!invalid.value) emit("submit", draft.value.trim() || (props.attachment?.kind === "dataset" ? "请分析这个数据文件。" : "预测这张 Inconel 625 EBSD 图片的屈服强度。"));
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
      <label for="materialsagent-message">{{ waitingQuestion ? '补充信息' : '消息' }}</label>
      <div class="composer__box">
      <section v-if="attachment || uploading || uploadError" class="composer__attachments" aria-label="已添加的附件">
        <div v-if="attachment" class="composer__attachment">
          <span class="attachment-name">{{ attachment.name }}</span>
          <button class="composer__remove" type="button" aria-label="移除附件" title="移除附件" :disabled="disabled" @click="$emit('remove-attachment')">×</button>
        </div>
        <p v-if="uploading" role="status">正在上传附件…</p>
        <p v-if="uploadError" class="field-error" role="alert">{{ uploadError }}</p>
        <button v-if="uploadError && canRetryUpload" class="button" type="button" :disabled="disabled" @click="$emit('check-upload')">核查原上传</button>
      </section>
      <textarea class="resize-none" id="materialsagent-message" v-model="draft" rows="4" maxlength="32768" :disabled="disabled"
        :aria-invalid="invalid" :aria-describedby="invalid ? 'composer-error' : undefined" @keydown="onKeydown" />
      <p v-if="invalid" id="composer-error" class="field-error" role="alert">请输入消息后再发送。</p>
      <div class="composer__toolbar">
      <input ref="fileInput" id="attachment-file" type="file" hidden accept=".csv,image/png,image/jpeg" :disabled="disabled" @change="chooseFile" />
      <button class="composer__add" type="button" aria-label="添加附件" title="添加附件" :disabled="disabled" @click="fileInput?.click()">+</button>
      <button class="button button--primary" type="submit" :disabled="disabled" :aria-busy="sending">{{ sending ? '正在执行…' : waitingQuestion ? '补充并继续' : '发送' }}</button>
      </div>
      </div>
    </form>
  </section>
</template>
