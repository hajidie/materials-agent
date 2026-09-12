import type { AssetSummary, ResultSummary } from "./types";
export interface Observation {
  observation_id: string;
  kind: "TOOL_RESULT" | "ARGUMENT_RESOLUTION";
  status: string;
  tool_name: string;
  data: Record<string, unknown>;
  artifacts: AssetSummary[];
  result_summary?: ResultSummary | null;
  warnings: unknown[];
  error: { code?: string; retryable?: boolean } | null;
  invocation_run_id: string | null;
}

export interface AgentExecution {
  invocation_run_id: string;
  tool_name: string;
  status: string;
  arguments: Record<string, unknown>;
  confirmation_version: string | null;
  confirmation_expires_at: string | null;
  retryable: boolean;
}

export interface AgentRun {
  agent_run_id: string;
  conversation_id: string;
  source_message_id: string;
  goal: string;
  status: "PENDING" | "RUNNING" | "WAITING_FOR_USER" | "WAITING_FOR_CONFIRMATION" | "SUCCEEDED" | "TERMINATED";
  version: number;
  waiting_version: number;
  waiting: { reason: "INTENT_CLARIFICATION" | "TOOL_ARGUMENT_CLARIFICATION"; question: string } | null;
  pending_execution: AgentExecution | null;
  executions: AgentExecution[];
  observations: Observation[];
  steps: Array<{ step_id: string; number: number; status: string; action: { type: string; tool_name?: string } | null }>;
  final_answer: { text: string; answer_id: string } | null;
  error_code: string | null;
  llm_tokens: number;
  tool_executions: number;
  active_seconds: number;
  user_inputs: string[];
  created_at: string;
}

export class AgentRequestError extends Error {
  constructor(public readonly uncertain: boolean, public readonly code: string) {
    super(code);
  }
}

export async function agentRequest<T>(path: string, options: { body?: unknown; key?: string; signal?: AbortSignal } = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method: options.body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", ...(options.key ? { "Idempotency-Key": options.key } : {}) },
      ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch {
    throw new AgentRequestError(options.body !== undefined, "NETWORK_UNCERTAIN");
  }
  let payload: { data?: T; error?: { code?: string } };
  try { payload = await response.json(); }
  catch { throw new AgentRequestError(options.body !== undefined, "RESPONSE_UNCERTAIN"); }
  if (!response.ok) throw new AgentRequestError(response.status >= 500, payload.error?.code ?? "REQUEST_FAILED");
  if (payload.data === undefined) throw new AgentRequestError(options.body !== undefined, "RESPONSE_UNCERTAIN");
  return payload.data;
}


export function toolLabel(name: string): string {
  return ({materials_unit_conversion: "材料单位换算", zta35g_sem_virtual_lab: "ZTA35G 虚拟实验"} as Record<string, string>)[name] ?? name;
}

export function clarificationLabel(question: string): string {
  const fields: Record<string, string> = {solution_temperature: "固溶温度", solution_time: "固溶时间", aging_temperature: "时效温度", aging_time: "时效时间", requested_outputs: "所需结果类型", from_unit: "原始单位", to_unit: "目标单位", value: "数值", material: "材料"};
  return question.replace(/\b(solution_temperature|solution_time|aging_temperature|aging_time|requested_outputs|from_unit|to_unit|value|material)\b/g, field => fields[field] ?? field);
}
