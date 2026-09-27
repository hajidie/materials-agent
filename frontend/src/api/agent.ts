import type { Attachment, Presentation } from "./artifacts";
export interface Observation {
  observation_id: string;
  kind: "TOOL_RESULT";
  status: string;
  tool_name: string;
  presentation: Presentation;
  artifacts: Attachment[];
}
export interface AgentExecution {
  invocation_run_id: string;
  tool_name: string;
  status: string;
  confirmation: Array<{ label: string; value: unknown }>;
  confirmation_version: string | null;
  confirmation_expires_at: string | null;
  retryable: boolean;
}
export interface AgentRun {
  agent_run_id: string;
  conversation_id: string;
  source_message_id: string;
  source_answer_message_id: string | null;
  answer_root_message_id: string | null;
  goal: string;
  status: "PENDING" | "RUNNING" | "WAITING_FOR_USER" | "WAITING_FOR_CONFIRMATION" | "SUCCEEDED" | "TERMINATED";
  version: number;
  waiting_version: number;
  waiting: { question: string } | null;
  submission_id: string | null;
  question_message_id: string | null;
  final_message_id: string | null;
  stopped: boolean;
  pending_execution: AgentExecution | null;
  executions: AgentExecution[];
  observations: Observation[];
  error_message: string | null;
  outcome_unknown: boolean;
  attachments: Attachment[];
  result_attachments: Attachment[];
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
      redirect: "error",
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
  return ({ebsd_yield_strength_predictor: "EBSD 屈服强度预测", materials_unit_conversion: "材料单位换算", zta35g_sem_virtual_lab: "ZTA35G 虚拟实验",
    materials_ml_analyze_tabular_dataset: "表格数据分析", materials_ml_train_tabular_regression: "模型训练",
    materials_ml_get_training_run: "训练状态查询", materials_ml_predict_with_model: "模型预测"} as Record<string, string>)[name] ?? name;
}

export interface ChatMessage {
  message_id: string;
  agent_run_id: string | null;
  role: "USER" | "ASSISTANT";
  phase: "user" | "question" | "answer" | "notification";
  content_status: "complete";
  text: string;
  sequence: number;
  created_at: string;
  attachments: Attachment[];
  artifacts: Attachment[];
  answer_root_message_id: string | null;
  answer_version: number | null;
  version_count: number;
}
export interface AcceptedSubmission {
  agent_run: AgentRun;
  submission_id: string;
  idempotency_replayed: boolean;
}
