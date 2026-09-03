export interface ApiSuccessEnvelope<T> {
  request_id: string;
  data: T;
}

export interface ApiErrorDetail {
  field: string;
  code: string;
  message: string;
}

export interface ApiResourceReference {
  conversation_id: string | null;
  task_id: string | null;
  tool_run_id: string | null;
  result_id: string | null;
}

export interface ApiErrorEnvelope {
  request_id: string;
  error: {
    code: string;
    message: string;
    details: ApiErrorDetail[];
  };
  resource: ApiResourceReference;
}

export interface Conversation {
  conversation_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationCreationData extends Conversation {
  idempotency_replayed: boolean;
}

export interface ConversationDeleteData {
  conversation_id: string;
}

export interface ConversationListItem extends Conversation {
  last_activity_preview: string | null;
}

export interface ConversationPage {
  items: ConversationListItem[];
  next_cursor: string | null;
}

export type TaskStatus =
  | "PENDING"
  | "RUNNING"
  | "NEEDS_INPUT"
  | "READY"
  | "SUCCEEDED"
  | "PARTIALLY_SUCCEEDED"
  | "FAILED";

export type ToolRunStatus =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "PARTIALLY_SUCCEEDED"
  | "FAILED";

export type ResultStatus =
  | "SUCCEEDED"
  | "PARTIALLY_SUCCEEDED"
  | "FAILED";

export type ExplanationStatus =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED";

export type TaskType = "KNOWLEDGE_QA" | "TOOL_EXECUTION" | null;

export interface UserMessage {
  message_id: string;
  role: "USER";
  content_text: string;
  created_at: string;
}

export interface AssistantMessage {
  message_id: string;
  role: "ASSISTANT";
  content_text: string;
  created_at: string;
}

export interface NeedsInput {
  missing_fields: string[];
  ambiguous_fields: Array<Record<string, unknown>>;
  normalized_input: Record<string, unknown> | null;
}

export interface ToolReference {
  tool_id: string;
  version: string;
  schema_hash: string;
}

export interface TaskNeedsInput extends NeedsInput {
  candidate_tool_refs: ToolReference[];
}

export interface SafeError {
  code: string;
  message: string;
}

export interface DiagnosticSummary {
  step: string | null;
  status: string | null;
  duration_ms: number | null;
  error_code: string | null;
  safe_error_message: string | null;
}

export interface ToolRunSummary {
  tool_run_id: string;
  attempt_no: number;
  tool_id: string;
  tool_version: string;
  schema_hash: string;
  status: ToolRunStatus;
  requested_outputs: string[];
  completed_outputs: string[];
  failed_outputs: string[];
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  diagnostics_summary: DiagnosticSummary[];
  error: SafeError | null;
  is_selected: boolean;
}

export interface ResultSummary {
  result_id: string;
  tool_run_id: string;
  status: ResultStatus;
  requested_outputs: string[];
  completed_outputs: string[];
  failed_outputs: string[];
  data: Record<string, unknown>;
  warnings: unknown[];
  provenance: Record<string, unknown>;
  error: Record<string, unknown> | null;
  tool_id: string;
  tool_version: string;
  schema_hash: string;
  created_at: string;
}

export interface AssetSummary {
  asset_id: string;
  status: "AVAILABLE";
  role: string;
  media_type: string;
  width: number;
  height: number;
  bit_depth: number;
  size_bytes: number;
  sha256: string;
  content_url: string;
}

export interface ExplanationSummary {
  explanation_id: string;
  attempt_no: number;
  status: "SUCCEEDED" | "FAILED";
  language: string;
  text: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  error_code: string | null;
  safe_error_message: string | null;
}

export interface TimelineTask {
  task_id: string;
  task_type: TaskType;
  status: TaskStatus;
  selected_tool_run_id: string | null;
  selected_result_id: string | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
  error_code: string | null;
  safe_error_message: string | null;
  tool_id: string | null;
  bound_tool_version: string | null;
  bound_schema_hash: string | null;
}

export interface TimelineUserMessageItem {
  item_type: "USER_MESSAGE";
  item_id: string;
  task_id: string;
  anchor_at: string;
  message: UserMessage;
}

export interface TimelineAssistantMessageItem {
  item_type: "ASSISTANT_MESSAGE";
  item_id: string;
  task_id: string;
  anchor_at: string;
  message: AssistantMessage;
}

export interface TimelineToolTaskItem {
  item_type: "TOOL_TASK";
  item_id: string;
  task_id: string;
  anchor_at: string;
  initial_user_message: UserMessage | null;
  task: TimelineTask;
  input_thread: Array<UserMessage | AssistantMessage>;
  input_thread_count: number;
  input_thread_truncated: boolean;
  tool_runs: {
    attempt_count: number;
    selected_tool_run: ToolRunSummary | null;
    has_history: boolean;
  };
  result: ResultSummary | null;
  assets: AssetSummary[];
  explanation: ExplanationSummary | null;
  latest_explanation_failure: ExplanationSummary | null;
  needs_input: TaskNeedsInput | null;
  errors: SafeError[];
}

export type TimelineItem =
  | TimelineUserMessageItem
  | TimelineAssistantMessageItem
  | TimelineToolTaskItem;

export interface TimelinePage {
  conversation: Pick<Conversation, "conversation_id" | "title">;
  items: TimelineItem[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface TaskDetail {
  task_id: string;
  conversation_id: string;
  task_type: TaskType;
  status: TaskStatus;
  anchor_at: string;
  selected_tool_run_id: string | null;
  selected_result_id: string | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
  error_code: string | null;
  safe_error_message: string | null;
  tool_id: string | null;
  bound_tool_version: string | null;
  bound_schema_hash: string | null;
  needs_input: TaskNeedsInput | null;
  tool_run_count: number;
  tool_runs: ToolRunSummary[];
  selected_result_summary: Pick<
    ResultSummary,
    | "result_id"
    | "status"
    | "requested_outputs"
    | "completed_outputs"
    | "failed_outputs"
  > | null;
  assets: AssetSummary[];
  explanation_summary: ExplanationSummary | null;
  latest_explanation_failure: ExplanationSummary | null;
}

export interface MessageSubmissionRequest {
  submission_mode: "NEW_TASK" | "SUPPLEMENT_TASK";
  content_text: string;
  target_task_id: string | null;
}

export interface MessageTask {
  task_id: string;
  task_type: TaskType;
  status: TaskStatus;
  selected_tool_run_id: string | null;
  selected_result_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface MessageExplanation {
  explanation_id: string;
  result_id: string;
  status: "SUCCEEDED" | "FAILED";
  language: string;
  text: string | null;
  error_code: string | null;
  safe_error_message: string | null;
  llm_call_id: string;
  llm_call_status: "SUCCEEDED" | "FAILED";
}

export interface MessageSubmissionResponseData {
  conversation_id: string;
  user_message: UserMessage;
  task: MessageTask;
  assistant_message: AssistantMessage | null;
  needs_input: NeedsInput | null;
  result_summary: Omit<
    ResultSummary,
    "tool_id" | "tool_version" | "schema_hash" | "created_at"
  > & {
    artifacts: AssetSummary[];
  } | null;
  explanation: MessageExplanation | null;
  latest_explanation_failure: MessageExplanation | null;
  idempotency_replayed: boolean;
}

export interface ToolRetryRequest {
  reason: string;
}

export interface ToolRetryResponseData {
  task_id: string;
  task_status: TaskStatus;
  tool_run: {
    tool_run_id: string;
    attempt_no: number;
    status: ToolRunStatus;
    task_input_revision_id: string;
  };
  result_id: string | null;
  result_status: ResultStatus | null;
  explanation_id: string | null;
  explanation_status: ExplanationStatus | null;
  idempotency_replayed: boolean;
}

export interface ExplanationRetryRequest {
  language: string;
  reason: string;
}

export interface ExplanationRetryResponseData {
  task_id: string;
  task_status: TaskStatus;
  explanation: {
    explanation_id: string;
    result_id: string;
    attempt_no: number;
    status: ExplanationStatus;
    language: string;
    text: string | null;
    error_code: string | null;
    safe_error_message: string | null;
    llm_call_id: string;
    llm_call_status: ExplanationStatus;
  };
  idempotency_replayed: boolean;
}

export interface ToolResult {
  result_id: string;
  task_id: string;
  tool_run_id: string;
  status: ResultStatus;
  requested_outputs: string[];
  completed_outputs: string[];
  failed_outputs: string[];
  data: Record<string, unknown>;
  artifacts: AssetSummary[];
  warnings: unknown[];
  tool_id: string;
  tool_version: string;
  schema_hash: string;
  provenance: Record<string, unknown>;
  error: Record<string, unknown> | null;
  created_at: string;
}

export interface AssetMetadata {
  asset_id: string;
  status: "PENDING" | "AVAILABLE" | "FAILED" | "ORPHANED";
  asset_type: string;
  source_type: string;
  producer_tool_run_id: string;
  role: string;
  media_type: string | null;
  width: number | null;
  height: number | null;
  bit_depth: number | null;
  size_bytes: number | null;
  sha256: string | null;
  created_at: string;
  available_at: string | null;
  error_code: string | null;
  safe_error_message: string | null;
}
