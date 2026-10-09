<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import type { Attachment } from "../api/artifacts";
import EbsdImage from "./EbsdImage.vue";
const props = defineProps<{ disabled: boolean; sending: boolean; generating?: boolean; stopping?: boolean; completed: number; waitingQuestion: string | null; initialDraft?: string; attachments?: Attachment[]; uploading?: boolean; uploadError?: string | null; canRetryUpload?: boolean; uploadEntries?: Array<{ key: string; name: string; status?: string; message?: string }> }>();
const emit = defineEmits<{ submit: [text: string]; stop: []; "update-draft": [text: string]; "upload": [files: File[]]; "remove-attachment": [identity: string]; "remove-upload": [key: string]; "check-upload": [key?: string] }>();
const draft = ref(props.initialDraft ?? "");
const invalid = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const messageInput = ref<HTMLTextAreaElement | null>(null);
const attachments = computed(() => props.attachments ?? []);
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
  const selected = Array.from(input.files ?? []);
  if (selected.length) { emit("upload", selected); }
  input.value = "";
}
watch(() => props.completed, () => { draft.value = ""; invalid.value = false; });
watch(() => props.initialDraft, value => { if (value && !draft.value) draft.value = value; });
watch(() => props.sending, sending => {
  if (sending) { draft.value = ""; invalid.value = false; }
  else if (props.initialDraft && !draft.value) draft.value = props.initialDraft;
});
function submit() {
  if (props.disabled || props.sending || props.generating) return;
  invalid.value = !draft.value.trim() && !attachments.value.length;
  if (!invalid.value) emit("submit", draft.value.trim() || (attachments.value[0]?.kind === "dataset" ? "请分析这个数据文件。" : "请根据上传图片进行分析；如果需要，请先询问分析目标。"));
}
function onKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); }
}
</script>
<template>
  <section class="composer" aria-label="发送消息">
    <p v-if="waitingQuestion" class="composer__question" role="status">回复上方问题即可继续。</p>
    <form class="composer__form" novalidate @submit.prevent="submit">
      <label class="visually-hidden" for="materialsagent-message">{{ waitingQuestion ? '补充信息' : '消息' }}</label>
      <div class="composer__box">
      <section v-if="attachments.length || uploading || uploadError || uploadEntries?.length" class="composer__attachments" aria-label="已添加的附件">
        <div v-for="attachment in attachments" :key="attachment.attachment_id" class="composer__attachment" :class="{ 'composer__attachment--image': ['image', 'ebsd_image'].includes(attachment.kind) }">
          <EbsdImage v-if="['image', 'ebsd_image'].includes(attachment.kind)" :asset-id="attachment.attachment_id" :image-label="attachment.name" preview />
          <span v-else class="attachment-name">{{ attachment.name }}</span>
          <button class="composer__remove" type="button" aria-label="移除附件" title="移除附件" :disabled="disabled" @click="$emit('remove-attachment', attachment.attachment_id)">×</button>
        </div>
        <div v-for="entry in uploadEntries" :key="entry.key" class="composer__upload-state">
          <span>{{ entry.name }}</span>
          <span :role="entry.status === 'failed' ? 'alert' : 'status'">{{ entry.message || '正在上传…' }}</span>
          <button v-if="entry.status === 'uncertain' && !uploading" class="button" type="button" :disabled="disabled" @click="$emit('check-upload', entry.key)">核查原上传</button>
          <button v-if="entry.status === 'failed'" class="button" type="button" :disabled="disabled" @click="$emit('remove-upload', entry.key)">移除</button>
        </div>
        <p v-if="uploading" role="status">正在上传附件…</p>
        <p v-if="uploadError" class="field-error" role="alert">{{ uploadError }}</p>
      </section>
      <div class="composer__input-row">
      <input ref="fileInput" id="attachment-file" type="file" hidden multiple accept=".csv,image/png,image/jpeg" :disabled="disabled" @change="chooseFile" />
      <button class="composer__add" type="button" aria-label="添加附件" title="添加附件" :disabled="disabled" @click="fileInput?.click()">
        <svg aria-hidden="true" viewBox="0 0 20 20" focusable="false"><path d="M10 3.75v12.5M3.75 10h12.5" /></svg>
      </button>
      <textarea ref="messageInput" class="resize-none" id="materialsagent-message" v-model="draft" rows="1" maxlength="32768" :disabled="disabled"
        :aria-invalid="invalid" :aria-describedby="invalid ? 'composer-error' : undefined" @input="resizeTextarea" @keydown="onKeydown" />
      <button v-if="generating" class="composer__stop" type="button" aria-label="中止生成" title="中止生成"
        :disabled="stopping" :aria-busy="stopping" @click="$emit('stop')">
        <svg aria-hidden="true" viewBox="0 0 24 24" focusable="false"><rect x="7" y="7" width="10" height="10" rx="1" /></svg>
      </button>
      <button v-else class="button button--primary composer__submit" type="submit" :disabled="disabled" aria-label="发送">
        <span>发送</span>
        <svg aria-hidden="true" viewBox="0 0 20 20" focusable="false"><path d="m4 10 11-6-3.25 12-2.2-4.35L4 10Zm5.55 1.65L15 4" /></svg>
      </button>
      </div>
      <p v-if="invalid" id="composer-error" class="field-error" role="alert">请输入消息后再发送。</p>
      </div>
    </form>
  </section>
</template>
