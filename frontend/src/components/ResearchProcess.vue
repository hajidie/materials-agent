<script lang="ts">
// Retain an explicit choice when the live card becomes a published message.
const expansionChoices = new Map<string, boolean>();
</script>
<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { agentRequest, toolLabel, type ProcessSnapshot } from "../api/agent";
import AssistantMarkdown from "./AssistantMarkdown.vue";
import ResultPresentation from "./ResultPresentation.vue";

const props = withDefaults(defineProps<{ runId: string; snapshot?: ProcessSnapshot | undefined; active?: boolean; showCurrent?: boolean; reconnecting?: boolean | undefined }>(), { active: false, showCurrent: false, reconnecting: false });
const stored = ref<ProcessSnapshot>();
const error = ref(false), loading = ref(false), expanded = ref(props.active), touched = ref(false);
let generation = 0;
async function load() {
  const epoch = ++generation;
  loading.value = true; error.value = false;
  try {
    const value = await agentRequest<ProcessSnapshot>(`/agent-runs/${encodeURIComponent(props.runId)}/process`);
    if (value.run?.agent_run_id !== props.runId || !Array.isArray(value.segments)) throw new Error("Invalid process snapshot");
    if (epoch === generation) stored.value = value;
  } catch { if (epoch === generation) error.value = true; }
  finally { if (epoch === generation) loading.value = false; }
}
watch(() => props.runId, () => { stored.value = undefined; touched.value = expansionChoices.has(props.runId); expanded.value = expansionChoices.get(props.runId) ?? props.active; void load(); }, { immediate: true });
watch(() => props.active, active => { if (!touched.value) expanded.value = active; });
const data = computed(() => props.snapshot?.run.agent_run_id === props.runId ? props.snapshot : stored.value);
const history = computed(() => data.value?.segments.filter(s => s.purpose === "process") ?? []);
const current = computed(() => props.showCurrent ? data.value?.segments.filter(s => s.kind === "text" && s.purpose !== "process").at(-1) : undefined);
function toggle() {
  touched.value = true; expanded.value = !expanded.value;
  expansionChoices.set(props.runId, expanded.value);
  if (expansionChoices.size > 128) expansionChoices.delete(expansionChoices.keys().next().value!);
}
function statusLabel(value?: string) {
  return ({ PREPARING: "准备调用", PREPARED: "已准备", PENDING: "准备执行", RUNNING: "正在执行", PENDING_CONFIRMATION: "等待确认", SUCCEEDED: "执行完成", FAILED: "执行失败", OUTCOME_UNKNOWN: "结果待核查", REJECTED: "已取消" } as Record<string, string>)[value ?? ""] ?? "处理记录";
}
</script>

<template>
  <div class="research-process">
    <details v-if="history.length || active || error" :open="expanded">
      <summary @click.prevent="toggle">研究过程<span class="muted">{{ active ? ' · 进行中' : ` · ${history.length} 项记录` }}</span></summary>
      <div v-if="expanded" class="research-process__body">
        <p v-if="loading && !data" class="muted">正在加载过程…</p>
        <p v-if="error && !data" class="field-error">过程暂时无法加载。<button class="button" @click="load">重试加载</button></p>
        <section v-for="segment in history" :key="segment.segment_id" class="research-process__step">
          <p class="research-process__label">{{ segment.kind === 'reasoning' ? '模型思考' : segment.kind === 'text' ? '行动说明' : `${statusLabel(segment.tool_status)} · ${toolLabel(segment.tool_name ?? '')}` }}<span v-if="segment.status === 'interrupted'"> · 未完成</span></p>
          <AssistantMarkdown v-if="segment.text" :text="segment.text" :complete="segment.status === 'complete'" compact />
          <ResultPresentation v-if="segment.presentation" :value="segment.presentation" />
        </section>
        <p v-if="active && !history.length" class="muted">正在分析请求…</p>
      </div>
    </details>
    <p v-if="reconnecting" class="muted" role="status">连接已断开，正在恢复查看；任务仍在后台处理。</p>
    <div v-if="current?.text" class="research-process__current">
      <p class="muted" role="status">{{ active ? '正在生成' : '正在载入回答' }}</p>
      <AssistantMarkdown :text="current.text" :complete="current.status === 'complete'" />
    </div>
  </div>
</template>
