<script setup lang="ts">
import {
  computed,
  defineComponent,
  h,
  type DeepReadonly,
  type PropType,
  type VNodeChild,
} from "vue";

import type { ResultSummary } from "../api/types";

defineOptions({ name: "StructuredResult" });

const props = defineProps<{
  result: ResultSummary | DeepReadonly<ResultSummary>;
}>();

const statusLabels: Record<ResultSummary["status"], string> = {
  SUCCEEDED: "已完成",
  PARTIALLY_SUCCEEDED: "部分完成",
  FAILED: "失败",
};

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

function renderStructuredValue(value: unknown, depth: number): VNodeChild {
  if (value === null) {
    return h("span", "—");
  }
  if (typeof value === "string") {
    return h("span", value);
  }
  if (typeof value === "number") {
    return h("span", Number.isFinite(value) ? String(value) : "—");
  }
  if (typeof value === "boolean") {
    return h("span", value ? "是" : "否");
  }
  if (depth >= 4 && (Array.isArray(value) || isPlainRecord(value))) {
    return h("span", "嵌套内容未展开");
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return h("span", "—");
    }
    return h(
      "ol",
      { class: "structured-value__array" },
      value.map((entry, index) =>
        h("li", { key: index }, [
          h(StructuredValue, { value: entry, depth: depth + 1 }),
        ]),
      ),
    );
  }
  if (isPlainRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      return h("span", "—");
    }
    return h(
      "dl",
      { class: "structured-value__object" },
      entries.map(([key, entry]) =>
        h("div", { key, class: "structured-value__field" }, [
          h("dt", key),
          h("dd", [
            h(StructuredValue, { value: entry, depth: depth + 1 }),
          ]),
        ]),
      ),
    );
  }
  return h("span", "内容未显示");
}

const StructuredValue = defineComponent({
  name: "StructuredValue",
  props: {
    value: {
      type: null as unknown as PropType<unknown>,
      required: true,
    },
    depth: {
      type: Number,
      required: true,
    },
  },
  setup(componentProps) {
    return () =>
      renderStructuredValue(componentProps.value, componentProps.depth);
  },
});

interface VisibleWarning {
  code: string | null;
  message: string;
}

const visibleWarnings = computed<VisibleWarning[]>(() =>
  props.result.warnings.map((warning) => {
    if (typeof warning === "string") {
      return { code: null, message: warning };
    }
    if (isPlainRecord(warning)) {
      const code = typeof warning.code === "string" ? warning.code : null;
      const message =
        typeof warning.message === "string" ? warning.message : null;
      if (code !== null || message !== null) {
        return {
          code,
          message: message ?? "结果包含一项附加提示",
        };
      }
    }
    return {
      code: null,
      message: "结果包含一项附加提示",
    };
  }),
);

const visibleError = computed(() => {
  const error = props.result.error;
  if (!isPlainRecord(error)) {
    return null;
  }
  const code = typeof error.code === "string" ? error.code : null;
  const message =
    typeof error.safe_message === "string"
      ? error.safe_message
      : typeof error.message === "string"
        ? error.message
        : null;
  return code === null && message === null ? null : { code, message };
});

function outputText(outputs: readonly string[]): string {
  return outputs.length > 0 ? outputs.join("、") : "无";
}
</script>

<template>
  <section class="result-panel" aria-label="结构化结果">
    <header class="section-heading">
      <div>
        <p class="eyebrow">结构化结果</p>
        <h3>{{ statusLabels[result.status] }}</h3>
      </div>
      <span class="status-badge" :data-status="result.status">
        {{ result.status }}
      </span>
    </header>

    <dl class="summary-grid">
      <div>
        <dt>请求输出</dt>
        <dd>{{ outputText(result.requested_outputs) }}</dd>
      </div>
      <div>
        <dt>已完成</dt>
        <dd>{{ outputText(result.completed_outputs) }}</dd>
      </div>
      <div>
        <dt>未完成</dt>
        <dd>{{ outputText(result.failed_outputs) }}</dd>
      </div>
    </dl>

    <div class="result-panel__data">
      <h4>结果数据</h4>
      <StructuredValue :value="result.data" :depth="0" />
    </div>

    <div v-if="visibleWarnings.length > 0" class="notice notice--warning">
      <h4>提示</h4>
      <ul>
        <li v-for="(warning, index) in visibleWarnings" :key="index">
          <strong v-if="warning.code">{{ warning.code }}：</strong>
          {{ warning.message }}
        </li>
      </ul>
    </div>

    <div v-if="visibleError" class="notice notice--error">
      <h4>结果错误</h4>
      <p>
        <strong v-if="visibleError.code">{{ visibleError.code }}：</strong>
        {{ visibleError.message || "结果未完成。" }}
      </p>
    </div>

    <details class="result-panel__details">
      <summary>来源与版本</summary>
      <dl class="summary-grid">
        <div>
          <dt>Tool</dt>
          <dd>{{ result.tool_id }}</dd>
        </div>
        <div>
          <dt>Tool version</dt>
          <dd>{{ result.tool_version }}</dd>
        </div>
        <div>
          <dt>Schema</dt>
          <dd>{{ result.schema_version }}</dd>
        </div>
      </dl>
      <h4>公共 provenance</h4>
      <StructuredValue :value="result.provenance" :depth="0" />
    </details>
  </section>
</template>
