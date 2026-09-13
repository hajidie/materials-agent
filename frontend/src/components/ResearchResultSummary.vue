<script setup lang="ts">
import { computed, type DeepReadonly } from "vue";

import EbsdImage from "./EbsdImage.vue";
import type { ResultSummary } from "../api/types";

defineOptions({ name: "ResearchResultSummary" });

const props = defineProps<{
  result: ResultSummary | DeepReadonly<ResultSummary>;
}>();

interface Measurement {
  value: number;
  unit: string;
}

interface LabeledMeasurement extends Measurement {
  label: string;
}

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

function measurement(value: unknown): Measurement | null {
  if (!isPlainRecord(value)) {
    return null;
  }
  if (
    typeof value.value !== "number" ||
    !Number.isFinite(value.value) ||
    typeof value.unit !== "string" ||
    value.unit.trim() === ""
  ) {
    return null;
  }
  return { value: value.value, unit: value.unit };
}

const conditions = computed<LabeledMeasurement[] | null>(() => {
  const provenance = props.result.provenance;
  const normalized = isPlainRecord(provenance.normalized_process_parameters)
    ? provenance.normalized_process_parameters
    : null;
  if (normalized === null) {
    return null;
  }

  const entries: Array<[string, string]> = [
    ["solution_temperature", "固溶温度"],
    ["solution_time", "固溶时间"],
    ["aging_temperature", "时效温度"],
    ["aging_time", "时效时间"],
  ];
  const parsed = entries.map(([key, label]) => {
    const value = measurement(normalized[key]);
    return value === null ? null : { label, ...value };
  });
  return parsed.every((entry) => entry !== null)
    ? (parsed as LabeledMeasurement[])
    : null;
});

const isEbsd = computed(() => props.result.tool_id === "ebsd_yield_strength_predictor");
const inputAssetId = computed(() => {
  const asset = props.result.provenance.input_asset;
  return isPlainRecord(asset) && typeof asset.asset_id === 'string' ? asset.asset_id : null;
});
const hasMechanicalProperties = computed(() =>
  props.result.completed_outputs.includes(isEbsd.value ? "yield_strength" : "mechanical_properties"),
);

const metrics = computed<LabeledMeasurement[] | null>(() => {
  if (!hasMechanicalProperties.value) {
    return null;
  }
  const elongation = measurement(props.result.data.elongation);
  const yieldStrength = measurement(props.result.data.yield_strength);
  if (isEbsd.value) return yieldStrength === null ? null : [{ label: "屈服强度", ...yieldStrength }];
  if (elongation === null || yieldStrength === null) {
    return null;
  }
  return [
    { label: "延伸率", ...elongation },
    { label: "屈服强度", ...yieldStrength },
  ];
});

function safeErrorMessage(value: unknown): string | null {
  if (!isPlainRecord(value)) {
    return null;
  }
  if (
    typeof value.safe_message === "string" &&
    value.safe_message.trim() !== ""
  ) {
    return value.safe_message;
  }
  return null;
}

const errorMessage = computed(() => safeErrorMessage(props.result.error));
</script>

<template>
  <section class="research-result-summary" aria-label="研究结果摘要">
    <section v-if="isEbsd" aria-label="EBSD 预测来源">
      <p>材料：Inconel 625</p>
      <EbsdImage v-if="inputAssetId" :asset-id="inputAssetId" />
      <p class="muted">实验性预测；图像编码要求与适用数据分布尚未核实。</p>
      <p>模型：{{ result.provenance.model_version }}</p>
    </section>
    <section v-else data-section="conditions">
      <h3>实验条件</h3>
      <dl v-if="conditions">
        <div v-for="condition in conditions" :key="condition.label">
          <dt>{{ condition.label }}</dt>
          <dd>{{ condition.value }} {{ condition.unit }}</dd>
        </div>
      </dl>
      <p v-else>实验条件暂不可用</p>
    </section>

    <section v-if="hasMechanicalProperties" data-section="metrics">
      <h3>关键性能</h3>
      <dl v-if="metrics">
        <div v-for="metric in metrics" :key="metric.label">
          <dt>{{ metric.label }}</dt>
          <dd>{{ metric.value.toFixed(2) }} {{ metric.unit }}</dd>
        </div>
      </dl>
      <p v-else>性能数据暂不可用</p>
    </section>

    <section v-if="errorMessage" aria-label="结果错误">
      <h3>结果错误</h3>
      <p>{{ errorMessage }}</p>
    </section>
  </section>
</template>
