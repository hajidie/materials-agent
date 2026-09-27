<script setup lang="ts">
import { computed } from "vue";
import ResultPresentation from "./ResultPresentation.vue";
import { toolLabel } from "../api/agent";
import type { AgentRun } from "../api/agent";
import type { Attachment } from "../api/artifacts";
const props = defineProps<{ run: AgentRun; disabled: boolean; reconciling?: boolean; receipt?: string | undefined }>();
defineEmits<{ confirm: [approved: boolean]; retry: [invocationId: string]; reconcile: [invocationId: string]; artifact: [value: { message: string; attachment: Attachment }] }>();
const terminal = computed(() => ["SUCCEEDED", "TERMINATED"].includes(props.run.status));
function valueText(value: unknown): string {
  if (Array.isArray(value)) return value.map(valueText).join("、");
  if (value && typeof value === "object") return Object.values(value).map(valueText).join("、");
  return value == null ? "未指定" : String(value);
}
</script>

<template>
  <article class="chat-turn">
    <section class="chat-assistant" aria-label="助手回复">
      <p v-if="run.status === 'RUNNING' || run.status === 'PENDING'" class="muted" role="status">{{ run.pending_execution?.status === "RUNNING" ? `正在执行${toolLabel(run.pending_execution.tool_name)}…` : "正在处理你的请求…" }}</p>
      <section v-if="run.status === 'WAITING_FOR_CONFIRMATION' && run.pending_execution" class="chat-confirmation">
        <p>将执行{{ toolLabel(run.pending_execution.tool_name) }}，请确认以下内容。</p>
        <dl class="artifact-facts"><template v-for="fact in run.pending_execution.confirmation" :key="fact.label"><dt>{{ fact.label }}</dt><dd>{{ valueText(fact.value) }}</dd></template></dl>
        <button class="button button--primary" :disabled="disabled" @click="$emit('confirm', true)">确认执行</button>
        <button class="button" :disabled="disabled" @click="$emit('confirm', false)">取消</button>
      </section>
      <template v-if="run.status === 'TERMINATED' && !run.stopped"><ResultPresentation v-for="observation in run.observations" :key="observation.observation_id" :value="observation.presentation" /></template>
      <p v-if="run.stopped" class="muted" role="status">{{ run.pending_execution?.status === "RUNNING" ? "已停止生成，已提交的计算仍在运行，完成后会保留结果。" : "已停止生成，已保存的内容仍保留。" }}</p>
      <p v-if="run.outcome_unknown" class="field-error" role="alert">暂时无法确认结果，请核查原操作。不会重复执行。</p>
      <p v-else-if="run.error_message && !run.stopped" class="field-error" role="alert">{{ run.error_message }}</p>
      <p v-if="receipt" role="status">{{ receipt }}</p>
      <div v-if="terminal" class="agent-run__actions">
        <template v-for="execution in run.executions" :key="execution.invocation_run_id">
          <button v-if="execution.status === 'OUTCOME_UNKNOWN'" class="button" :disabled="reconciling" @click="$emit('reconcile', execution.invocation_run_id)">核查原操作</button>
          <button v-if="!run.outcome_unknown && execution.status === 'FAILED' && execution.retryable" class="button" :disabled="disabled" @click="$emit('retry', execution.invocation_run_id)">重试</button>
        </template>
      </div>
    </section>
  </article>
</template>
