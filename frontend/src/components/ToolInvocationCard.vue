<script setup lang="ts">
import { computed, type DeepReadonly } from "vue";

import type { ToolInvocation } from "../api/types";

defineOptions({ name: "ToolInvocationCard" });

const props = defineProps<{
  invocation: ToolInvocation | DeepReadonly<ToolInvocation>;
  mutationBusy: boolean;
}>();

defineEmits<{
  confirm: [invocationRunId: string];
  reject: [invocationRunId: string];
}>();

const statusText = computed(() => ({
  PENDING: "等待执行",
  PENDING_CONFIRMATION: "等待确认",
  RUNNING: "执行中",
  SUCCEEDED: "已完成",
  FAILED: "执行失败",
  DENIED: "权限拒绝",
  REJECTED: "已拒绝",
  EXPIRED: "确认已过期",
  OUTCOME_UNKNOWN: "结果未知",
}[props.invocation.status]));

const presentation = computed(() => props.invocation.result?.presentation ?? null);
const resultTitle = computed(() => {
  const value = presentation.value?.title;
  return typeof value === "string" ? value : null;
});
const resultSummary = computed(() => {
  const value = presentation.value?.summary;
  return typeof value === "string" ? value : null;
});
</script>

<template>
  <article
    class="invocation-card"
    :data-invocation-status="invocation.status"
  >
    <header>
      <div>
        <p class="invocation-card__eyebrow">
          {{ invocation.tool.execution_profile }} TOOL
        </p>
        <h3>{{ invocation.tool.display_name }}</h3>
      </div>
      <span class="invocation-card__status">{{ statusText }}</span>
    </header>

    <p v-if="invocation.status === 'PENDING_CONFIRMATION'">
      {{ invocation.tool.confirmation_prompt ?? "此操作会产生外部副作用。确认后平台会再次校验权限，再执行一次。" }}
    </p>
    <p v-if="resultTitle"><strong>{{ resultTitle }}</strong></p>
    <p v-if="resultSummary">{{ resultSummary }}</p>
    <p v-if="invocation.safe_error_message" class="invocation-card__error">
      {{ invocation.safe_error_message }}
    </p>

    <div
      v-if="invocation.status === 'PENDING_CONFIRMATION'"
      class="invocation-card__actions"
    >
      <button
        type="button"
        class="button"
        data-action="confirm-tool-invocation"
        :disabled="mutationBusy"
        @click="$emit('confirm', invocation.invocation_run_id)"
      >
        确认执行
      </button>
      <button
        type="button"
        class="button button--secondary"
        data-action="reject-tool-invocation"
        :disabled="mutationBusy"
        @click="$emit('reject', invocation.invocation_run_id)"
      >
        拒绝
      </button>
    </div>
  </article>
</template>

<style scoped>
.invocation-card {
  display: grid;
  gap: 0.75rem;
  padding: 1rem;
  border: 1px solid var(--color-border, #d8d4cb);
  border-left: 4px solid #2f766f;
  border-radius: 0.75rem;
  background: var(--color-surface, #fff);
}

.invocation-card header,
.invocation-card__actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.invocation-card h3,
.invocation-card p {
  margin: 0;
}

.invocation-card__eyebrow {
  color: #52706c;
  font-size: 0.75rem;
  letter-spacing: 0.08em;
}

.invocation-card__status {
  white-space: nowrap;
  font-size: 0.875rem;
}

.invocation-card__error {
  color: #9c332d;
}

.invocation-card__actions {
  justify-content: flex-start;
}
</style>
