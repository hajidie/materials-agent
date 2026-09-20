export interface ApiSuccessEnvelope<T> {
  request_id: string;
  data: T;
}

export interface ApiErrorDetail {
  field: string;
  code: string;
  message: string;
  operation_id?: string;
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

export type ResultStatus =
  | "SUCCEEDED"
  | "PARTIALLY_SUCCEEDED"
  | "FAILED";

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
