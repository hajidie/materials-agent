<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import ResultPresentation from "./ResultPresentation.vue";
import ResearchProcess from "./ResearchProcess.vue";
import { toolLabel } from "../api/agent";
import type { AgentRun, ProcessSnapshot } from "../api/agent";
import type { Attachment } from "../api/artifacts";
const props = defineProps<{ run: AgentRun; disabled: boolean; reconciling?: boolean; receipt?: string | undefined; process?: ProcessSnapshot | undefined; reconnecting?: boolean | undefined }>();
defineEmits<{ confirm: [approved: boolean]; resume: []; stop: []; retry: [invocationId: string]; reconcile: [invocationId: string]; artifact: [value: { message: string; attachment: Attachment }] }>();
const terminal = computed(() => ["SUCCEEDED", "TERMINATED"].includes(props.run.status));
const records = computed(() => [...props.run.executions, ...(props.run.pending_execution ? [props.run.pending_execution] : [])]);
const instant = ref(Date.now());
let timer: ReturnType<typeof setInterval>;
onMounted(() => { timer = setInterval(() => { instant.value = Date.now(); }, 1000); });
onUnmounted(() => clearInterval(timer));
const continueAction = computed(() => ['CONTINUE', 'FIX_CONFIGURATION'].includes(props.run.recovery_action ?? 'CONTINUE'));
const ready = computed(() => props.run.can_resume === true || (continueAction.value && !!props.run.resume_after && instant.value >= Date.parse(props.run.resume_after)) || (props.run.can_resume === undefined && !props.run.outcome_unknown));
function valueText(value: unknown): string {
  if (Array.isArray(value)) return value.map(valueText).join("、");
  if (value && typeof value === "object") return Object.values(value).map(valueText).join("、");
  return value == null ? "未指定" : String(value);
}
</script>

<template>
  <article class="chat-turn">
    <section class="chat-assistant" aria-label="助手回复">
      <ResearchProcess :run-id="run.agent_run_id" :snapshot="process" :active="['PENDING', 'RUNNING'].includes(run.status)" show-current :reconnecting="reconnecting" />
      <p v-if="run.status === 'RUNNING' || run.status === 'PENDING'" class="muted" role="status">{{ run.pending_execution?.status === "RUNNING" ? `正在执行${toolLabel(run.pending_execution.tool_name)}…` : "正在处理你的请求…" }}</p>
      <section v-if="run.status === 'WAITING_FOR_CONFIRMATION' && run.pending_execution" class="chat-confirmation">
        <p>将执行{{ toolLabel(run.pending_execution.tool_name) }}，请确认以下内容。</p>
        <dl class="artifact-facts"><template v-for="fact in run.pending_execution.confirmation" :key="fact.label"><dt>{{ fact.label }}</dt><dd>{{ valueText(fact.value) }}</dd></template></dl>
        <button class="button button--primary" :disabled="disabled" @click="$emit('confirm', true)">确认执行</button>
        <button class="button" :disabled="disabled" @click="$emit('confirm', false)">取消</button>
      </section>
      <template v-if="['TERMINATED', 'INTERRUPTED'].includes(run.status) && !run.stopped"><ResultPresentation v-for="observation in run.observations" :key="observation.observation_id" :value="observation.presentation" /></template>
      <p v-if="run.stopped" class="muted" role="status">{{ run.pending_execution?.status === "RUNNING" ? "已停止生成，已提交的计算仍在运行，完成后会保留结果。" : "已停止生成，已保存的内容仍保留。" }}</p>
      <p v-if="run.outcome_unknown" class="field-error" role="alert">暂时无法确认结果，请核查原操作。不会重复执行。</p>
      <p v-else-if="run.error_message && !run.stopped" class="field-error" role="alert">{{ run.error_message }}</p>
      <div v-if="run.status === 'INTERRUPTED'" class="agent-run__actions">
        <button v-if="continueAction" class="button button--primary" :disabled="disabled || !ready" @click="$emit('resume')">{{ !ready && run.resume_after ? '等待服务允许重试' : run.recovery_action === 'FIX_CONFIGURATION' ? '修正后继续处理' : '继续处理' }}</button>
        <button v-if="run.recovery_action === 'RECONCILE' && run.pending_execution" class="button" :disabled="disabled || reconciling" @click="$emit('reconcile', run.pending_execution.invocation_run_id)">核查原操作</button>
        <button class="button" :disabled="disabled" @click="$emit('stop')">结束本次处理</button>
      </div>
      <p v-if="receipt" role="status">{{ receipt }}</p>
      <div v-if="terminal" class="agent-run__actions">
        <template v-for="execution in records" :key="execution.invocation_run_id">
          <button v-if="execution.status === 'OUTCOME_UNKNOWN'" class="button" :disabled="reconciling" @click="$emit('reconcile', execution.invocation_run_id)">核查原操作</button>
          <button v-if="!run.outcome_unknown && execution.status === 'FAILED' && execution.retryable" class="button" :disabled="disabled" @click="$emit('retry', execution.invocation_run_id)">重试</button>
        </template>
      </div>
    </section>
  </article>
</template>
