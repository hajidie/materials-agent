<script setup lang="ts">
import { computed, ref, type DeepReadonly } from "vue";

import type {
  TaskDetail,
  TaskStatus,
  TimelineToolTaskItem,
} from "../api/types";
import AssetGallery from "./AssetGallery.vue";
import StructuredResult from "./StructuredResult.vue";
import TaskHistory from "./TaskHistory.vue";

defineOptions({ name: "ToolTaskCard" });

const props = defineProps<{
  item:
    | TimelineToolTaskItem
    | DeepReadonly<TimelineToolTaskItem>;
  conversationId: string;
  mutationBusy: boolean;
  taskDetail?:
    | TaskDetail
    | DeepReadonly<TaskDetail>
    | undefined;
  historyLoading: boolean;
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
  "load-history": [taskId: string];
}>();

const statusLabels: Record<TaskStatus, string> = {
  PENDING: "等待执行",
  RUNNING: "执行中",
  NEEDS_INPUT: "等待补充",
  SUCCEEDED: "已完成",
  PARTIALLY_SUCCEEDED: "部分完成",
  FAILED: "失败",
};

const historyOpen = ref(false);

const supplementSummary = computed(() => {
  const missing = props.item.needs_input?.missing_fields ?? [];
  return missing.length > 0
    ? `待补充：${missing.join("、")}`
    : "工具任务需要补充信息";
});

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
  if (result === null) {
    return false;
  }
  const explanation = props.item.explanation;
  return (
    explanation === null ||
    explanation.status === "FAILED" ||
    props.item.latest_explanation_failure !== null
  );
});

function safeRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  try {
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function ambiguityText(value: Record<string, unknown>): string {
  const field = typeof value.field === "string" ? value.field : null;
  const message =
    typeof value.message === "string"
      ? value.message
      : "存在一项歧义待确认。";
  return field ? `${field}：${message}` : message;
}

function summaryValue(value: unknown): string {
  if (value === null) {
    return "—";
  }
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return String(value);
  }
  if (
    Array.isArray(value) &&
    value.every(
      (entry) =>
        typeof entry === "string" ||
        typeof entry === "number" ||
        typeof entry === "boolean",
    )
  ) {
    return value.join("、");
  }
  return safeRecord(value) === null ? "内容未显示" : "已提供结构化值";
}

function recordEntries(
  value: unknown,
): [string, unknown][] | null {
  const record = safeRecord(value);
  if (record === null) {
    return null;
  }
  try {
    return Object.entries(record);
  } catch {
    return null;
  }
}

function outputText(outputs: readonly string[]): string {
  return outputs.length > 0 ? outputs.join("、") : "无";
}

function requestSupplement(): void {
  emit("supplement", {
    conversationId: props.conversationId,
    taskId: props.item.task_id,
    summary: supplementSummary.value,
  });
}

function openHistory(): void {
  historyOpen.value = true;
  if (props.taskDetail === undefined && !props.historyLoading) {
    emit("load-history", props.item.task_id);
  }
}
</script>

<template>
  <article class="tool-card" :aria-label="`工具任务 ${item.task_id}`">
    <header class="section-heading">
      <div>
        <p class="eyebrow">材料工具任务</p>
        <h2>{{ statusLabels[item.task.status] }}</h2>
      </div>
      <span class="status-badge" :data-status="item.task.status">
        {{ item.task.status }}
      </span>
    </header>

    <section class="tool-card__section">
      <h3>初始请求</h3>
      <p class="tool-card__request">
        {{ item.initial_user_message?.content_text || "未提供初始请求" }}
      </p>
    </section>

    <div
      v-if="item.task.safe_error_message || item.errors.length > 0"
      class="notice notice--error"
      role="alert"
    >
      <p v-if="item.task.safe_error_message">
        {{ item.task.safe_error_message }}
      </p>
      <ul v-if="item.errors.length > 0">
        <li v-for="error in item.errors" :key="`${error.code}:${error.message}`">
          {{ error.message }}
        </li>
      </ul>
    </div>

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
            {{ field }}
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
            {{ ambiguityText(ambiguous) }}
          </li>
        </ul>
      </div>
      <div v-if="item.needs_input.normalized_input">
        <h4>已识别输入摘要</h4>
        <dl class="summary-grid">
          <div
            v-for="(value, key) in item.needs_input.normalized_input"
            :key="key"
          >
            <dt>{{ key }}</dt>
            <dd>
              <dl
                v-if="recordEntries(value)"
                class="structured-value__object"
              >
                <div
                  v-for="entry in recordEntries(value) ?? []"
                  :key="entry[0]"
                  class="structured-value__field"
                >
                  <dt>{{ entry[0] }}</dt>
                  <dd>
                    <dl
                      v-if="recordEntries(entry[1])"
                      class="structured-value__object"
                    >
                      <div
                        v-for="nestedEntry in
                          recordEntries(entry[1]) ?? []"
                        :key="nestedEntry[0]"
                        class="structured-value__field"
                      >
                        <dt>{{ nestedEntry[0] }}</dt>
                        <dd>
                          <span v-if="recordEntries(nestedEntry[1])">
                            嵌套内容未展开
                          </span>
                          <span v-else>
                            {{ summaryValue(nestedEntry[1]) }}
                          </span>
                        </dd>
                      </div>
                    </dl>
                    <span v-else>{{ summaryValue(entry[1]) }}</span>
                  </dd>
                </div>
              </dl>
              <span v-else>{{ summaryValue(value) }}</span>
            </dd>
          </div>
        </dl>
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

    <section v-if="item.tool_runs.selected_tool_run" class="tool-card__section">
      <h3>当前 ToolRun</h3>
      <dl class="summary-grid">
        <div>
          <dt>尝试</dt>
          <dd>{{ item.tool_runs.selected_tool_run.attempt_no }}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{{ item.tool_runs.selected_tool_run.status }}</dd>
        </div>
        <div>
          <dt>请求输出</dt>
          <dd>
            {{ outputText(item.tool_runs.selected_tool_run.requested_outputs) }}
          </dd>
        </div>
        <div>
          <dt>已完成</dt>
          <dd>
            {{ outputText(item.tool_runs.selected_tool_run.completed_outputs) }}
          </dd>
        </div>
        <div>
          <dt>未完成</dt>
          <dd>
            {{ outputText(item.tool_runs.selected_tool_run.failed_outputs) }}
          </dd>
        </div>
        <div>
          <dt>耗时</dt>
          <dd>
            {{
              item.tool_runs.selected_tool_run.duration_ms === null
                ? "—"
                : `${item.tool_runs.selected_tool_run.duration_ms} ms`
            }}
          </dd>
        </div>
      </dl>
      <ul
        v-if="
          item.tool_runs.selected_tool_run.diagnostics_summary.length > 0
        "
        class="diagnostic-list"
      >
        <li
          v-for="(diagnostic, index) in
            item.tool_runs.selected_tool_run.diagnostics_summary"
          :key="index"
        >
          {{ diagnostic.step || "未命名步骤" }}：
          {{ diagnostic.status || "状态未知" }}
          <span v-if="diagnostic.duration_ms !== null">
            · {{ diagnostic.duration_ms }} ms
          </span>
          <span v-if="diagnostic.safe_error_message">
            · {{ diagnostic.safe_error_message }}
          </span>
        </li>
      </ul>
      <p
        v-if="item.tool_runs.selected_tool_run.error"
        class="notice notice--error"
      >
        {{ item.tool_runs.selected_tool_run.error.message }}
      </p>
    </section>

    <StructuredResult v-if="item.result" :result="item.result" />
    <AssetGallery :assets="item.assets" />

    <section
      v-if="item.explanation || item.latest_explanation_failure"
      class="tool-card__section explanation-panel"
    >
      <div v-if="item.explanation">
        <h3>结果解释</h3>
        <p v-if="item.explanation.text" class="explanation-panel__text">
          {{ item.explanation.text }}
        </p>
        <p
          v-else-if="item.explanation.safe_error_message"
          class="notice notice--error"
        >
          {{ item.explanation.safe_error_message }}
        </p>
      </div>
      <div
        v-if="item.latest_explanation_failure"
        class="notice notice--error"
      >
        <h4>最近一次解释重试失败</h4>
        <p>
          {{
            item.latest_explanation_failure.safe_error_message ||
            "解释重试未完成。"
          }}
        </p>
      </div>
    </section>

    <div class="tool-card__actions">
      <button
        v-if="canRetryTool"
        type="button"
        class="button button--secondary"
        data-action="retry-tool"
        :disabled="mutationBusy"
        @click="$emit('retry-tool', item.task_id)"
      >
        重试工具
      </button>
      <button
        v-if="canRetryExplanation && item.result"
        type="button"
        class="button button--secondary"
        data-action="retry-explanation"
        :disabled="mutationBusy"
        @click="$emit('retry-explanation', item.result.result_id)"
      >
        重试解释
      </button>
      <button
        v-if="item.tool_runs.has_history && !historyOpen"
        type="button"
        class="button button--text"
        data-action="load-history"
        :disabled="historyLoading"
        @click="openHistory"
      >
        {{ historyLoading ? "加载历史中…" : "查看运行历史" }}
      </button>
    </div>

    <TaskHistory
      v-if="historyOpen"
      :detail="taskDetail"
      :loading="historyLoading"
      @close="historyOpen = false"
    />
  </article>
</template>
