<script setup lang="ts">
import { ref, watch } from "vue";
const props = defineProps<{ disabled: boolean; sending: boolean; completed: number; waitingQuestion: string | null; initialDraft?: string }>();
const emit = defineEmits<{ submit: [text: string]; "cancel-resume": [] }>();
const draft = ref(props.initialDraft ?? "");
const invalid = ref(false);
watch(() => props.completed, () => { draft.value = ""; invalid.value = false; });
watch(() => props.initialDraft, value => { if (value && !draft.value) draft.value = value; });
function submit() {
  if (props.disabled || props.sending) return;
  invalid.value = !draft.value.trim();
  if (!invalid.value) emit("submit", draft.value.trim());
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
      <textarea id="materialsagent-message" v-model="draft" rows="4" maxlength="32768" :disabled="disabled"
        :aria-invalid="invalid" :aria-describedby="invalid ? 'composer-error' : 'composer-hint'" @keydown="onKeydown" />
      <p id="composer-hint" class="composer__hint">Enter 发送，Shift+Enter 换行</p>
      <p v-if="invalid" id="composer-error" class="field-error" role="alert">请输入消息后再发送。</p>
      <button class="button button--primary" type="submit" :disabled="disabled" :aria-busy="sending">{{ sending ? '正在执行…' : waitingQuestion ? '补充并继续' : '发送目标' }}</button>
    </form>
  </section>
</template>
