<script setup lang="ts">
import { computed } from "vue";
import ResearchResultSummary from "./ResearchResultSummary.vue";
import EbsdImage from "./EbsdImage.vue";
import AssetGallery from "./AssetGallery.vue";
import { clarificationLabel, toolLabel } from "../api/agent";
import type { AgentRun } from "../api/agent";
const props = defineProps<{ run: AgentRun; disabled: boolean }>();
defineEmits<{ resume: []; confirm: [approved: boolean]; regenerate: []; retry: [invocationId: string] }>();
const labels = { PENDING: "等待执行", RUNNING: "正在执行", WAITING_FOR_USER: "等待补充", WAITING_FOR_CONFIRMATION: "等待确认", SUCCEEDED: "已完成", TERMINATED: "已结束" };
const terminal = computed(() => ["SUCCEEDED", "TERMINATED"].includes(props.run.status));
const canRegenerate = computed(() => terminal.value && props.run.observations.some(o => o.kind === "TOOL_RESULT" && o.status === "SUCCEEDED"));
</script>

<template>
  <article class="tool-card agent-run" :data-run-id="run.agent_run_id">
    <header class="agent-run__header"><h3>{{ run.goal }}</h3><span class="status-badge" role="status">{{ labels[run.status] }}</span></header>
    <EbsdImage v-if="run.ebsd_asset_id && !run.observations.some(o => o.result_summary)" :asset-id="run.ebsd_asset_id" />
    <p v-if="run.status === 'RUNNING'" class="muted">正在处理目标，进度会自动更新。材料模型推理可能需要较长时间。</p>
    <p v-for="(input, i) in run.user_inputs" :key="i" class="agent-run__input">补充：{{ input }}</p>
    <section v-if="run.waiting && run.status === 'WAITING_FOR_USER'" class="supplement-banner">
      <div><strong>{{ run.waiting.reason === 'INTENT_CLARIFICATION' ? '请明确研究目标' : '请补充工具参数' }}</strong><p>{{ clarificationLabel(run.waiting.question) }}</p></div>
      <button class="button button--primary" :disabled="disabled" @click="$emit('resume')">补充信息</button>
    </section>
    <section v-if="run.status === 'WAITING_FOR_CONFIRMATION' && run.pending_execution" class="supplement-banner">
      <div><strong>确认执行 {{ run.pending_execution.tool_name }}</strong><pre>{{ JSON.stringify(run.pending_execution.arguments, null, 2) }}</pre></div>
      <button class="button button--primary" :disabled="disabled" @click="$emit('confirm', true)">确认执行</button>
      <button class="button" :disabled="disabled" @click="$emit('confirm', false)">拒绝执行</button>
    </section>
    <section v-for="observation in run.observations.filter(o => o.kind === 'TOOL_RESULT')" :key="observation.observation_id" class="tool-card__section">
      <h4>{{ toolLabel(observation.tool_name) }} · {{ observation.status === 'SUCCEEDED' ? '执行成功' : observation.status === 'PARTIALLY_SUCCEEDED' ? '部分成功' : '执行失败' }}</h4>
      <ResearchResultSummary v-if="observation.result_summary" :result="observation.result_summary" />
      <pre v-else>{{ JSON.stringify(observation.data, null, 2) }}</pre>
      <AssetGallery :assets="observation.artifacts" />
    </section>
    <section v-if="run.final_answer" class="tool-card__section"><h4>最终回答</h4><p class="agent-answer">{{ run.final_answer.text }}</p></section>
    <p v-if="run.error_code" class="field-error" role="alert">运行已结束：{{ run.error_code }}。已保存的结果仍可查看。</p>
    <div v-if="terminal" class="agent-run__actions">
      <button v-if="canRegenerate" class="button" :disabled="disabled" @click="$emit('regenerate')">重新生成回答</button>
      <template v-for="execution in run.executions" :key="execution.invocation_run_id">
        <button v-if="execution.status === 'FAILED' && execution.retryable" class="button" :disabled="disabled" @click="$emit('retry', execution.invocation_run_id)">重试失败的工具调用</button>
      </template>
    </div>
    <details class="agent-trace"><summary>查看执行过程 · {{ run.steps.length }} 步 · {{ run.tool_executions }} 次工具调用</summary>
      <ol><li v-for="step in run.steps" :key="step.step_id">{{ step.action?.type ?? '决策' }} {{ step.action?.tool_name ?? '' }} · {{ step.status }}</li></ol>
      <p>累计 Token：{{ run.llm_tokens }}（包含必要的保守估算） · 活跃时间：{{ Math.round(run.active_seconds) }} 秒</p>
    </details>
  </article>
</template>
