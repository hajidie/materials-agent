<script setup lang="ts">
import { computed, type DeepReadonly } from "vue";

import type { UserVisibleError } from "../api/errors";
import type {
  MutationStatus,
  PendingMutationV1,
} from "../composables/useIdempotentRequest";

defineOptions({ name: "GlobalErrorNotice" });

type VisibleError =
  | UserVisibleError
  | DeepReadonly<UserVisibleError>;

const props = defineProps<{
  error?: VisibleError | null;
  errors?: readonly VisibleError[];
  pendingMutation:
    | PendingMutationV1
    | DeepReadonly<PendingMutationV1>
    | null;
  mutationStatus: MutationStatus;
}>();

function noticeKey(error: VisibleError): string {
  return JSON.stringify([
    error.message,
    error.status ?? null,
    error.request_id ?? null,
  ]);
}

const visibleErrors = computed<VisibleError[]>(() => {
  const candidates = [...(props.errors ?? [])];
  if (props.error !== undefined && props.error !== null) {
    candidates.push(props.error);
  }
  const seen = new Set<string>();
  return candidates.filter((candidate) => {
    const key = noticeKey(candidate);
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
});

defineEmits<{
  "retry-pending": [];
  "discard-pending": [];
}>();
</script>

<template>
  <section
    v-if="visibleErrors.length > 0 || pendingMutation"
    class="global-notices"
    aria-label="请求状态"
  >
    <div
      v-for="error in visibleErrors"
      :key="noticeKey(error)"
      class="global-notice global-notice--error"
      role="alert"
    >
      <strong>{{ error.message }}</strong>
      <p v-if="error.status">HTTP {{ error.status }}</p>
      <p v-if="error.request_id">request_id：{{ error.request_id }}</p>
      <ul v-if="error.details && error.details.length > 0">
        <li
          v-for="detail in error.details"
          :key="`${detail.field}:${detail.code}:${detail.message}`"
        >
          <strong>{{ detail.field }}</strong>
          <span>（{{ detail.code }}）：{{ detail.message }}</span>
        </li>
      </ul>
    </div>

    <div
      v-if="pendingMutation"
      class="global-notice global-notice--uncertain"
      role="alert"
    >
      <strong>未能确认上一次操作结果。</strong>
      <p>
        可以使用原请求重试；放弃恢复记录不会取消服务器端可能已经完成的操作。
      </p>
      <div class="global-notice__actions">
        <button
          type="button"
          class="button button--primary"
          data-action="retry-pending"
          :disabled="mutationStatus === 'SENDING'"
          @click="$emit('retry-pending')"
        >
          {{
            mutationStatus === "SENDING"
              ? "正在重试…"
              : "使用原请求重试"
          }}
        </button>
        <button
          type="button"
          class="button button--secondary"
          data-action="discard-pending"
          :disabled="mutationStatus === 'SENDING'"
          @click="$emit('discard-pending')"
        >
          放弃前端恢复记录
        </button>
      </div>
    </div>
  </section>
</template>
