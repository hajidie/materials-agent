<script setup lang="ts">
import { computed, type DeepReadonly } from "vue";

import type {
  ResultStatus,
  TaskStatus,
  TimelineToolTaskItem,
} from "../api/types";
import AssetGallery from "./AssetGallery.vue";
import ResearchResultSummary from "./ResearchResultSummary.vue";
import ToolInvocationCard from "./ToolInvocationCard.vue";

defineOptions({ name: "ToolTaskCard" });

const props = defineProps<{
  item: TimelineToolTaskItem | DeepReadonly<TimelineToolTaskItem>;
  conversationId: string;
  mutationBusy: boolean;
}>();

const emit = defineEmits<{
  supplement: [
    target: {
      conversationId: string;
      taskId: string;
      summary: string;
    },
  ];
  "retry-tool": [taskId: string];
  "retry-explanation": [resultId: string];
  "confirm-invocation": [invocationRunId: string];
  "reject-invocation": [invocationRunId: string];
}>();

const fieldLabels: Record<string, string> = {
  material: "材料",
  solution_temperature: "固溶温度",
  solution_time: "固溶时间",
  aging_temperature: "时效温度",
  aging_time: "时效时间",
};

const outputLabels: Record<string, string> = {
  sem_image: "组织图像",
  mechanical_properties: "力学性能",
};

const taskStatusBadges: Record<TaskStatus, string> = {
  PENDING: "生成中",
  RUNNING: "生成中",
  NEEDS_INPUT: "待补充",
  READY: "待执行",
  SUCCEEDED: "已完成",
  PARTIALLY_SUCCEEDED: "部分完成",
  FAILED: "失败",
};

const resultStatusBadges: Record<ResultStatus, string> = {
  SUCCEEDED: "已完成",
  PARTIALLY_SUCCEEDED: "部分完成",
  FAILED: "失败",
};

function fieldLabel(field: string): string {
  return fieldLabels[field] ?? "待补充信息";
}

const supplementSummary = computed(() => {
  const missing = props.item.needs_input?.missing_fields ?? [];
  return missing.length > 0
    ? `待补充：${missing.map(fieldLabel).join("、")}`
    : "需要补充信息";
});

type StatusTone = "success" | "partial" | "pending" | "attention" | "error";

function statusTone(status: TaskStatus | ResultStatus): StatusTone {
  switch (status) {
    case "SUCCEEDED":
      return "success";
    case "PARTIALLY_SUCCEEDED":
      return "partial";
    case "PENDING":
    case "READY":
    case "RUNNING":
      return "pending";
    case "NEEDS_INPUT":
      return "attention";
    case "FAILED":
      return "error";
  }
}

const isTaskActive = computed(
  () =>
    props.item.task.status === "PENDING" ||
    props.item.task.status === "READY" ||
    props.item.task.status === "RUNNING",
);

const displayTone = computed(() =>
  statusTone(
    isTaskActive.value
      ? props.item.task.status
      : (props.item.result?.status ?? props.item.task.status),
  ),
);

const statusBadge = computed(() => {
  if (isTaskActive.value) {
    return taskStatusBadges[props.item.task.status];
  }
  const result = props.item.result;
  return result
    ? resultStatusBadges[result.status]
    : taskStatusBadges[props.item.task.status];
});

const title = computed(() => {
  if (isTaskActive.value) {
    return "正在生成组织图像…";
  }
  const result = props.item.result;
  if (result) {
    if (result.status === "PARTIALLY_SUCCEEDED") {
      return "部分结果已生成";
    }
    if (result.status === "FAILED") {
      return "生成失败";
    }
    if (
      result.completed_outputs.includes("sem_image") &&
      props.item.assets.length > 0
    ) {
      return "组织图像已生成";
    }
    if (result.completed_outputs.includes("mechanical_properties")) {
      return "性能预测已完成";
    }
    return "结果已生成";
  }

  switch (props.item.task.status) {
    case "PENDING":
    case "RUNNING":
      return "正在生成组织图像…";
    case "NEEDS_INPUT":
      return "需要补充信息";
    case "FAILED":
      return "生成失败";
    case "PARTIALLY_SUCCEEDED":
      return "部分结果已生成";
    case "SUCCEEDED":
      return "结果已生成";
  }
});

const failedOutputLabels = computed(() =>
  (props.item.result?.failed_outputs ?? [])
    .map((output) => outputLabels[output])
    .filter((label): label is string => label !== undefined),
);

const canRetryTool = computed(() => {
  const status = props.item.task.status;
  if (
    status === "NEEDS_INPUT" ||
    status === "PENDING" ||
    status === "RUNNING" ||
    status === "SUCCEEDED"
  ) {
    return false;
  }
  const result = props.item.result;
  return (
    result === null ||
    result.status === "FAILED" ||
    result.status === "PARTIALLY_SUCCEEDED"
  );
});

const canRetryExplanation = computed(() => {
  const result = props.item.result;
  if (result === null || isTaskActive.value) {
    return false;
  }
  const explanation = props.item.explanation;
  return (
    explanation?.status === "FAILED" ||
    props.item.latest_explanation_failure !== null
  );
});

const isExplanationPendingOrUnknown = computed(() => {
  const result = props.item.result;
  return (
    result !== null &&
    result.status !== "FAILED" &&
    props.item.explanation === null &&
    props.item.latest_explanation_failure === null
  );
});

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  try {
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  } catch {
    return false;
  }
}

function ambiguityText(value: Record<string, unknown>): string {
  const field = typeof value.field === "string" ? value.field : null;
  const message =
    typeof value.message === "string"
      ? value.message
      : "存在一项歧义待确认。";
  return field ? `${fieldLabel(field)}：${message}` : message;
}

function requestSupplement(): void {
  emit("supplement", {
    conversationId: props.conversationId,
    taskId: props.item.task_id,
    summary: supplementSummary.value,
  });
}
</script>

<template>
  <article class="tool-card" aria-label="材料实验结果">
    <header class="section-heading">
      <div>
        <p class="eyebrow">实验结果</p>
        <h2>{{ title }}</h2>
      </div>
      <span class="status-badge" :data-tone="displayTone">
        {{ statusBadge }}
      </span>
    </header>

    <ToolInvocationCard
      v-if="item.invocation"
      :invocation="item.invocation"
      :mutation-busy="mutationBusy"
      @confirm="$emit('confirm-invocation', $event)"
      @reject="$emit('reject-invocation', $event)"
    />

    <template v-if="item.result">
      <AssetGallery :assets="item.assets" />
      <ResearchResultSummary :result="item.result" />

      <p
        v-if="failedOutputLabels.length > 0"
        class="tool-card__result-notice notice notice--warning"
      >
        未能生成：{{ failedOutputLabels.join("、") }}
      </p>

      <section
        class="tool-card__section explanation-panel"
        data-section="explanation"
      >
        <h3>结果说明</h3>
        <p
          v-if="item.explanation?.text"
          class="explanation-panel__text"
        >
          {{ item.explanation.text }}
        </p>
        <p
          v-else-if="isTaskActive || isExplanationPendingOrUnknown"
          class="explanation-panel__text"
        >
          结果说明生成中…
        </p>
        <template v-else>
          <p class="explanation-panel__text">结果说明暂不可用。</p>
          <p
            v-if="item.explanation?.safe_error_message"
            class="notice notice--error"
          >
            {{ item.explanation.safe_error_message }}
          </p>
        </template>
        <div
          v-if="item.latest_explanation_failure"
          class="notice notice--error"
        >
          <h4>最近一次重新生成说明失败</h4>
          <p>
            {{
              item.latest_explanation_failure.safe_error_message ||
              "说明重新生成未完成。"
            }}
          </p>
        </div>
      </section>
    </template>

    <template v-else>
      <section class="tool-card__section">
        <h3>初始请求</h3>
        <p class="tool-card__request">
          {{ item.initial_user_message?.content_text || "未提供初始请求" }}
        </p>
      </section>

      <section
        v-if="item.input_thread.length > 0 || item.input_thread_truncated"
        class="tool-card__section"
      >
        <h3>补充与追问</h3>
        <p v-if="item.input_thread_truncated" class="notice notice--info">
          较早的部分补充记录未在当前卡片中展开。
        </p>
        <ol class="input-thread">
          <li
            v-for="message in item.input_thread"
            :key="message.message_id"
            data-input-message
          >
            <strong>{{ message.role === "USER" ? "用户" : "智能体" }}</strong>
            <p>{{ message.content_text }}</p>
          </li>
        </ol>
      </section>

      <section
        v-if="item.task.status === 'NEEDS_INPUT' && item.needs_input"
        class="tool-card__section needs-input"
      >
        <h3>需要补充的信息</h3>
        <div>
          <h4>缺失字段</h4>
          <ul>
            <li v-for="field in item.needs_input.missing_fields" :key="field">
              {{ fieldLabel(field) }}
            </li>
          </ul>
        </div>
        <div v-if="item.needs_input.ambiguous_fields.length > 0">
          <h4>需要确认</h4>
          <ul>
            <li
              v-for="(ambiguous, index) in item.needs_input.ambiguous_fields"
              :key="index"
            >
              {{
                ambiguityText(
                  isPlainRecord(ambiguous) ? ambiguous : {},
                )
              }}
            </li>
          </ul>
        </div>
        <button
          type="button"
          class="button button--primary"
          data-action="supplement"
          :disabled="mutationBusy"
          @click="requestSupplement"
        >
          补充信息
        </button>
      </section>
    </template>

    <div
      v-if="item.task.safe_error_message || item.errors.length > 0"
      class="tool-card__safe-errors notice notice--error"
      role="alert"
    >
      <p v-if="item.task.safe_error_message">
        {{ item.task.safe_error_message }}
      </p>
      <ul v-if="item.errors.length > 0">
        <li v-for="(error, index) in item.errors" :key="index">
          {{ error.message }}
        </li>
      </ul>
    </div>

    <div v-if="canRetryTool || canRetryExplanation" class="tool-card__actions">
      <button
        v-if="canRetryTool"
        type="button"
        class="button button--secondary"
        data-action="retry-tool"
        :disabled="mutationBusy"
        @click="$emit('retry-tool', item.task_id)"
      >
        重新生成结果
      </button>
      <button
        v-if="canRetryExplanation && item.result"
        type="button"
        class="button button--secondary"
        data-action="retry-explanation"
        :disabled="mutationBusy"
        @click="$emit('retry-explanation', item.result.result_id)"
      >
        重新生成说明
      </button>
    </div>
  </article>
</template>
