<script setup lang="ts">
import type { Presentation } from "../api/artifacts";
defineProps<{ value: Presentation; details?: boolean }>();
const labels: Record<string, string> = { display_name: "文件名", row_count: "样本数量", target: "预测目标", target_unit: "目标单位", model_type: "模型类型", features: "输入变量", test_size: "测试集比例", duplicate_row_count: "重复行数", units: "变量单位", value: "数值", unit: "单位", input_value: "原始数值", input_unit: "原始单位", prediction_preview: "预测结果预览", row: "行" };
function text(value: unknown): string {
  if (Array.isArray(value)) return value.map(text).join("、");
  if (value && typeof value === "object") return Object.entries(value).map(([key, item]) => `${labels[key] ?? key}：${text(item)}`).join("；");
  return value == null ? "未声明" : String(value);
}
</script>
<template>
  <section class="result-presentation">
    <p>{{ value.summary }}</p>
    <dl v-if="value.metrics.length" class="artifact-metrics"><div v-for="metric in value.metrics" :key="metric.label"><dt>{{ metric.label }}</dt><dd>{{ Number(metric.value.toPrecision(5)) }} {{ metric.unit }}</dd></div></dl>
    <dl v-if="details" class="artifact-facts"><template v-for="(fact, key) in value.facts" :key="key"><template v-if="key !== 'columns' && key !== 'prediction_preview'"><dt>{{ labels[key] ?? key }}</dt><dd>{{ text(fact) }}</dd></template></template></dl>
    <div v-if="details && Array.isArray(value.facts.columns)" class="artifact-table-wrap"><table><caption>数据列概况</caption><thead><tr><th>变量</th><th>类型</th><th>缺失数量</th></tr></thead><tbody><tr v-for="(column, i) in (value.facts.columns as Record<string, unknown>[])" :key="i"><td>{{ column.name }}</td><td>{{ column.dtype }}</td><td>{{ column.missing_count ?? '—' }}</td></tr></tbody></table></div>
    <div v-if="details && Array.isArray(value.facts.prediction_preview)" class="artifact-table-wrap"><table><caption>预测结果预览</caption><thead><tr><th>行</th><th>预测值<span v-if="value.facts.target_unit">（{{ value.facts.target_unit }}）</span></th></tr></thead><tbody><tr v-for="row in (value.facts.prediction_preview as { row: number; value: number }[])" :key="row.row"><td>{{ row.row }}</td><td>{{ Number(row.value.toPrecision(6)) }}</td></tr></tbody></table></div>
    <p v-for="note in value.notes" :key="note" class="muted">{{ note }}</p>
  </section>
</template>
