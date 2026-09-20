<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";

defineOptions({ name: "DeleteConversationDialog" });

const props = defineProps<{
  title: string;
  pending: boolean;
  error: string | null;
  reconciling?: boolean;
}>();

const emit = defineEmits<{
  cancel: [];
  confirm: [];
}>();

const dialog = ref<HTMLElement | null>(null);
const cancelButton = ref<HTMLButtonElement | null>(null);
let restoreFocusTo: HTMLElement | null = null;

onMounted(() => {
  restoreFocusTo = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  void nextTick(() => cancelButton.value?.focus());
});

onBeforeUnmount(() => {
  restoreFocusTo?.focus();
});

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && !props.pending) {
    event.preventDefault();
    emit("cancel");
    return;
  }
  if (event.key !== "Tab" || dialog.value === null) {
    return;
  }
  const focusable = Array.from(
    dialog.value.querySelectorAll<HTMLElement>(
      "button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
    ),
  );
  if (focusable.length === 0) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
</script>

<template>
  <div class="dialog-backdrop" @mousedown.self="!pending && $emit('cancel')">
    <section
      ref="dialog"
      class="delete-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="delete-dialog-title"
      aria-describedby="delete-dialog-description"
      @keydown="onKeydown"
    >
      <p class="eyebrow">永久删除</p>
      <h2 id="delete-dialog-title">删除“{{ title }}”？</h2>
      <p id="delete-dialog-description">
        这会永久删除该对话、消息、任务、结果与图片记录，并协调清理该对话的 ML 资源，且无法恢复。活动工作不会被自动取消。
      </p>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
      <div class="delete-dialog__actions">
        <button
          ref="cancelButton"
          type="button"
          class="button button--secondary"
          :disabled="pending"
          @click="$emit('cancel')"
        >
          取消
        </button>
        <button
          type="button"
          class="button button--danger"
          data-action="confirm-delete-conversation"
          :disabled="pending"
          @click="$emit('confirm')"
        >
          {{ pending ? (reconciling ? "正在核查…" : "正在删除…") : reconciling ? "核查原删除" : "永久删除" }}
        </button>
      </div>
    </section>
  </div>
</template>
