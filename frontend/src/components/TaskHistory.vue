<script setup lang="ts">
import type { DeepReadonly } from "vue";

import type {
  TaskDetail,
  ToolRunStatus,
} from "../api/types";

defineOptions({ name: "TaskHistory" });

defineProps<{
  detail?: TaskDetail | DeepReadonly<TaskDetail> | undefined;
  loading: boolean;
}>();

defineEmits<{
  close: [];
}>();

const statusLabels: Record<ToolRunStatus, string> = {
  PENDING: "等待执行",
  RUNNING: "执行中",
  SUCCEEDED: "已完成",
  PARTIALLY_SUCCEEDED: "部分完成",
  FAILED: "失败",
};

function outputText(outputs: readonly string[]): string {
  return outputs.length > 0 ? outputs.join("、") : "无";
}

function formatTime(value: string | null): string {
  if (value === null) {
    return "—";
  }
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return value;
  }
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(timestamp));
  } catch {
    return value;
  }
}
</script>

<template>
  <section class="task-history" aria-label="工具运行历史">
    <header class="section-heading">
      <h3>运行历史</h3>
      <button
        type="button"
        class="button button--text"
        data-action="close-history"
        @click="$emit('close')"
      >
        关闭
      </button>
    </header>

    <p v-if="loading" class="loading-text" role="status">
      正在加载运行历史…
    </p>
    <p v-else-if="!detail" class="empty-state">
      尚未加载运行历史。
    </p>
    <p v-else-if="detail.tool_runs.length === 0" class="empty-state">
      这个任务还没有 ToolRun。
    </p>

    <ol v-else class="history-list">
      <li
        v-for="toolRun in detail.tool_runs"
        :key="toolRun.tool_run_id"
        class="history-attempt"
        :class="{ 'history-attempt--selected': toolRun.is_selected }"
        :data-history-attempt="String(toolRun.attempt_no)"
      >
        <header>
          <strong>尝试 {{ toolRun.attempt_no }}</strong>
          <span v-if="toolRun.is_selected" class="status-badge">
            当前选中
          </span>
          <span class="status-badge" :data-status="toolRun.status">
            {{ statusLabels[toolRun.status] }}
          </span>
        </header>
        <dl class="summary-grid">
          <div>
            <dt>创建</dt>
            <dd>{{ formatTime(toolRun.created_at) }}</dd>
          </div>
          <div>
            <dt>开始</dt>
            <dd>{{ formatTime(toolRun.started_at) }}</dd>
          </div>
          <div>
            <dt>完成</dt>
            <dd>{{ formatTime(toolRun.completed_at) }}</dd>
          </div>
          <div>
            <dt>耗时</dt>
            <dd>
              {{
                toolRun.duration_ms === null
                  ? "—"
                  : `${toolRun.duration_ms} ms`
              }}
            </dd>
          </div>
          <div>
            <dt>请求输出</dt>
            <dd>{{ outputText(toolRun.requested_outputs) }}</dd>
          </div>
          <div>
            <dt>已完成</dt>
            <dd>{{ outputText(toolRun.completed_outputs) }}</dd>
          </div>
          <div>
            <dt>未完成</dt>
            <dd>{{ outputText(toolRun.failed_outputs) }}</dd>
          </div>
        </dl>
        <ul
          v-if="toolRun.diagnostics_summary.length > 0"
          class="diagnostic-list"
        >
          <li
            v-for="(diagnostic, index) in toolRun.diagnostics_summary"
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
        <p v-if="toolRun.error" class="notice notice--error">
          {{ toolRun.error.message }}
        </p>
      </li>
    </ol>
    <p v-if="detail" class="task-history__scope">
      历史仅展示 ToolRun 安全摘要；旧尝试的完整 Result 正文不在此复制。
    </p>
  </section>
</template>
