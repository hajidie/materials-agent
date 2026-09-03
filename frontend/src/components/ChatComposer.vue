<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { MutationStatus } from "../composables/useIdempotentRequest";
import type { SupplementTarget } from "../composables/useMaterialsAgent";

defineOptions({ name: "ChatComposer" });

const props = defineProps<{
  disabled: boolean;
  sending: boolean;
  supplementTarget: SupplementTarget | null;
  mutationStatus: MutationStatus;
  initialDraft?: string;
}>();

const emit = defineEmits<{
  "submit-new-task": [contentText: string];
  "submit-supplement": [contentText: string];
  "cancel-supplement": [];
}>();

const draft = ref("");
const validationMessage = ref<string | null>(null);
const awaitingMutation = ref(false);
const inputDisabled = computed(
  () => props.disabled || props.sending || awaitingMutation.value,
);

watch(draft, (value) => {
  if (value.trim()) {
    validationMessage.value = null;
  }
});

watch(
  () => props.initialDraft,
  (value) => {
    if (typeof value === "string" && value && draft.value !== value) {
      draft.value = value;
    }
  },
  { immediate: true },
);

watch(
  () => props.mutationStatus,
  (status) => {
    if (!awaitingMutation.value || status === "SENDING") {
      return;
    }
    if (status === "SUCCEEDED") {
      draft.value = "";
    }
    if (
      status === "SUCCEEDED" ||
      status === "BUSINESS_FAILED" ||
      status === "UNCERTAIN" ||
      status === "IDLE"
    ) {
      awaitingMutation.value = false;
    }
  },
);

function submit(): void {
  if (inputDisabled.value) {
    return;
  }
  const contentText = draft.value.trim();
  if (!contentText) {
    validationMessage.value = "请输入消息后再发送。";
    return;
  }
  validationMessage.value = null;
  awaitingMutation.value = true;
  if (props.supplementTarget === null) {
    emit("submit-new-task", contentText);
  } else {
    emit("submit-supplement", contentText);
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== "Enter" || event.shiftKey) {
    return;
  }
  event.preventDefault();
  submit();
}
</script>

<template>
  <section class="composer" aria-label="发送消息">
    <div
      v-if="supplementTarget"
      class="supplement-banner"
      aria-label="补充目标"
    >
      <div>
        <strong>正在为指定任务补充信息</strong>
        <p>{{ supplementTarget.summary }}</p>
      </div>
      <button
        type="button"
        class="button button--text"
        data-action="cancel-supplement"
        :disabled="sending"
        @click="$emit('cancel-supplement')"
      >
        取消补充
      </button>
    </div>

    <form class="composer__form" @submit.prevent="submit">
      <label for="materialsagent-message">消息</label>
      <textarea
        id="materialsagent-message"
        v-model="draft"
        rows="4"
        :disabled="inputDisabled"
        :placeholder="
          supplementTarget
            ? '补充这个任务所需的信息'
            : '输入材料知识问题或工具请求'
        "
        @keydown="onKeydown"
      />
      <p class="composer__hint">Enter 发送，Shift+Enter 换行</p>
      <p v-if="validationMessage" class="field-error" role="alert">
        {{ validationMessage }}
      </p>
      <button
        type="submit"
        class="button button--primary"
        :disabled="inputDisabled"
      >
        {{ sending ? "发送中…" : supplementTarget ? "提交补充" : "发送" }}
      </button>
    </form>
  </section>
</template>
