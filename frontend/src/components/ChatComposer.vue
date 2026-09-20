<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import type { Attachment } from "../api/artifacts";
import EbsdImage from "./EbsdImage.vue";
const props = defineProps<{ disabled: boolean; sending: boolean; completed: number; waitingQuestion: string | null; initialDraft?: string; attachment?: Attachment | undefined; uploading?: boolean; uploadError?: string | null; canRetryUpload?: boolean }>();
const emit = defineEmits<{ submit: [text: string]; "cancel-resume": []; "update-draft": [text: string]; "upload": [file: File]; "remove-attachment": []; "check-upload": [] }>();
const draft = ref(props.initialDraft ?? "");
const invalid = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const messageInput = ref<HTMLTextAreaElement | null>(null);
const imageAttachment = computed(() => props.attachment && ["ebsd_image", "image"].includes(props.attachment.kind) ? props.attachment : null);
const MIN_TEXTAREA_HEIGHT = 40;
const MAX_TEXTAREA_HEIGHT = 192;
function resizeTextarea() {
  const input = messageInput.value;
  if (!input) return;
  input.style.height = "auto";
  const contentHeight = input.scrollHeight;
  input.style.height = `${Math.min(Math.max(contentHeight, MIN_TEXTAREA_HEIGHT), MAX_TEXTAREA_HEIGHT)}px`;
  input.style.overflowY = contentHeight > MAX_TEXTAREA_HEIGHT ? "auto" : "hidden";
}
watch(draft, value => { emit("update-draft", value); void nextTick(resizeTextarea); });
onMounted(() => { void nextTick(resizeTextarea); });
function chooseFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const selected = input.files?.[0];
  if (selected) { emit("upload", selected); }
  input.value = "";
}
watch(() => props.completed, () => { draft.value = ""; invalid.value = false; });
watch(() => props.initialDraft, value => { if (value && !draft.value) draft.value = value; });
watch(() => props.sending, sending => {
  if (sending) { draft.value = ""; invalid.value = false; }
  else if (props.initialDraft && !draft.value) draft.value = props.initialDraft;
});
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
      <label class="visually-hidden" for="materialsagent-message">{{ waitingQuestion ? '补充信息' : '消息' }}</label>
      <div class="composer__box">
      <section v-if="attachment || uploading || uploadError" class="composer__attachments" aria-label="已添加的附件">
        <div v-if="attachment" class="composer__attachment" :class="{ 'composer__attachment--image': imageAttachment }">
          <EbsdImage v-if="imageAttachment" :asset-id="imageAttachment.attachment_id" :image-label="imageAttachment.name" preview />
          <span v-else class="attachment-name">{{ attachment.name }}</span>
          <button class="composer__remove" type="button" aria-label="移除附件" title="移除附件" :disabled="disabled" @click="$emit('remove-attachment')">×</button>
        </div>
        <p v-if="uploading" role="status">正在上传附件…</p>
        <p v-if="uploadError" class="field-error" role="alert">{{ uploadError }}</p>
        <button v-if="uploadError && canRetryUpload" class="button" type="button" :disabled="disabled" @click="$emit('check-upload')">核查原上传</button>
      </section>
      <div class="composer__input-row">
      <input ref="fileInput" id="attachment-file" type="file" hidden accept=".csv,image/png,image/jpeg" :disabled="disabled" @change="chooseFile" />
      <button class="composer__add" type="button" aria-label="添加附件" title="添加附件" :disabled="disabled" @click="fileInput?.click()">
        <svg aria-hidden="true" viewBox="0 0 20 20" focusable="false"><path d="M10 3.75v12.5M3.75 10h12.5" /></svg>
      </button>
      <textarea ref="messageInput" class="resize-none" id="materialsagent-message" v-model="draft" rows="1" maxlength="32768" :disabled="disabled"
        :aria-invalid="invalid" :aria-describedby="invalid ? 'composer-error' : undefined" @input="resizeTextarea" @keydown="onKeydown" />
      <button class="button button--primary composer__submit" type="submit" :disabled="disabled" :aria-busy="sending">
        <span>{{ sending ? '正在执行…' : waitingQuestion ? '补充并继续' : '发送' }}</span>
        <svg v-if="!sending" aria-hidden="true" viewBox="0 0 20 20" focusable="false"><path d="m4 10 11-6-3.25 12-2.2-4.35L4 10Zm5.55 1.65L15 4" /></svg>
      </button>
      </div>
      <p v-if="invalid" id="composer-error" class="field-error" role="alert">请输入消息后再发送。</p>
      </div>
    </form>
  </section>
</template>
