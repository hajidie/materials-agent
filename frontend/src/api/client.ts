import {
  ApiResponseError,
  ConversationCreationUncertaintyError,
  NetworkUncertaintyError,
  ProtocolResponseError,
  ReadNetworkError,
  RequestAbortedError,
} from "./errors";
import type {
  ApiErrorDetail,
  ApiResourceReference,
  ApiSuccessEnvelope,
  AssetMetadata,
  ConversationDeleteData,
  Conversation,
  ConversationPage,
  ToolResult,
} from "./types";

const DEFAULT_API_BASE_URL = "/api/v1";
const PUBLIC_URL_ORIGIN = "http://materialsagent.invalid";

interface RequestOptions {
  networkFailure:
    | "READ"
    | "IDEMPOTENT_WRITE"
    | "CONVERSATION_CREATE";
  body?: Record<string, unknown>;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

export interface MaterialsAgentApi {
  createConversation(
    title?: string,
    idempotencyKey?: string,
  ): Promise<ApiSuccessEnvelope<Conversation>>;
  deleteConversation?(
    conversationId: string,
  ): Promise<ApiSuccessEnvelope<ConversationDeleteData>>;
  listConversations(
    limit?: number,
    cursor?: string,
    signal?: AbortSignal,
  ): Promise<ApiSuccessEnvelope<ConversationPage>>;
  getToolResult(
    resultId: string,
    signal?: AbortSignal,
  ): Promise<ApiSuccessEnvelope<ToolResult>>;
  getAssetMetadata(
    assetId: string,
    signal?: AbortSignal,
  ): Promise<ApiSuccessEnvelope<AssetMetadata>>;
  resolvePublicContentUrl(contentUrl: string): string;
}

export interface CreateMaterialsAgentApiOptions {
  fetchImpl?: typeof fetch;
  baseUrl?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeApiBase(value: string): string {
  const candidate = value.trim();
  if (!candidate) {
    throw new Error("API base URL must not be empty.");
  }
  if (candidate.startsWith("/")) {
    if (candidate.startsWith("//") || candidate.includes("?") || candidate.includes("#")) {
      throw new Error("API base URL must be root-relative or use http/https.");
    }
    const normalized = candidate.replace(/\/+$/, "");
    if (!normalized) {
      throw new Error("API base URL must identify an API path.");
    }
    return normalized;
  }

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error("API base URL must be root-relative or use http/https.");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("API base URL must use http or https.");
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("API base URL contains unsupported URL components.");
  }
  parsed.pathname = parsed.pathname.replace(/\/+$/, "");
  return parsed.toString().replace(/\/$/, "");
}

function encodeId(value: string): string {
  return encodeURIComponent(value);
}

function withQuery(
  path: string,
  values: Record<string, string | number | undefined>,
): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined) {
      query.set(key, String(value));
    }
  }
  const rendered = query.toString();
  return rendered ? `${path}?${rendered}` : path;
}

function parseResource(value: unknown): ApiResourceReference | null {
  if (!isRecord(value)) {
    return null;
  }
  const keys = [
    "conversation_id",
    "task_id",
    "tool_run_id",
    "result_id",
  ] as const;
  const result: ApiResourceReference = {
    conversation_id: null,
    task_id: null,
    tool_run_id: null,
    result_id: null,
  };
  for (const key of keys) {
    const item = value[key];
    if (item !== null && typeof item !== "string") {
      return null;
    }
    result[key] = item;
  }
  return result;
}

function parseDetails(value: unknown): ApiErrorDetail[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const details: ApiErrorDetail[] = [];
  for (const item of value) {
    if (isRecord(item) && Object.keys(item).length === 1 && typeof item.operation_id === "string") {
      details.push({ field: "operation_id", code: "COORDINATION_OPERATION", message: item.operation_id, operation_id: item.operation_id });
      continue;
    }
    if (
      !isRecord(item) ||
      typeof item.field !== "string" ||
      typeof item.code !== "string" ||
      typeof item.message !== "string"
    ) {
      return null;
    }
    details.push({
      field: item.field,
      code: item.code,
      message: item.message,
    });
  }
  return details;
}

function parseApiError(status: number, payload: unknown): ApiResponseError {
  if (
    !isRecord(payload) ||
    typeof payload.request_id !== "string" ||
    !isRecord(payload.error) ||
    typeof payload.error.code !== "string" ||
    typeof payload.error.message !== "string"
  ) {
    throw new ProtocolResponseError();
  }
  const details = parseDetails(payload.error.details);
  const resource = parseResource(payload.resource);
  if (details === null || resource === null) {
    throw new ProtocolResponseError();
  }
  return new ApiResponseError(
    status,
    payload.request_id,
    payload.error.code,
    payload.error.message,
    details,
    resource,
  );
}

function parseSuccess<T>(payload: unknown): ApiSuccessEnvelope<T> {
  if (
    !isRecord(payload) ||
    typeof payload.request_id !== "string" ||
    !Object.hasOwn(payload, "data")
  ) {
    throw new ProtocolResponseError();
  }
  return {
    request_id: payload.request_id,
    data: payload.data as T,
  };
}

function isAbortError(error: unknown): boolean {
  return (
    error instanceof DOMException && error.name === "AbortError"
  ) || (
    isRecord(error) && error.name === "AbortError"
  );
}

function parsePublicAssetContentUrl(value: string): URL {
  const candidate = value.trim();
  if (
    !candidate.startsWith("/") ||
    candidate.startsWith("//")
  ) {
    throw new Error("Public content URL is invalid.");
  }
  const parsed = new URL(candidate, PUBLIC_URL_ORIGIN);
  if (parsed.hash) {
    throw new Error("Public content URL is invalid.");
  }
  const segments = parsed.pathname.split("/");
  if (
    segments.length !== 6 ||
    segments[1] !== "api" ||
    segments[2] !== "v1" ||
    segments[3] !== "assets" ||
    !segments[4] ||
    segments[5] !== "content"
  ) {
    throw new Error("Public content URL must identify Asset content.");
  }
  return parsed;
}

function resolvePublicContentUrlAgainstBase(
  contentUrl: string,
  apiBase: string,
): string {
  const content = parsePublicAssetContentUrl(contentUrl);
  const path = `${content.pathname}${content.search}`;
  if (apiBase.startsWith("/")) {
    return path;
  }
  const apiOrigin = new URL(apiBase).origin;
  return new URL(path, apiOrigin).toString();
}

export function resolvePublicContentUrl(contentUrl: string): string {
  return resolvePublicContentUrlAgainstBase(
    contentUrl,
    normalizeApiBase(
      import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE_URL,
    ),
  );
}

export function withAttachmentDisposition(contentUrl: string): string {
  const parsed = parsePublicAssetContentUrl(contentUrl);
  parsed.searchParams.set("disposition", "attachment");
  return `${parsed.pathname}${parsed.search}`;
}

export function createMaterialsAgentApi(
  options: CreateMaterialsAgentApiOptions = {},
): MaterialsAgentApi {
  const apiBase = normalizeApiBase(
    options.baseUrl ??
      import.meta.env.VITE_API_BASE_URL ??
      DEFAULT_API_BASE_URL,
  );
  const fetchImpl =
    options.fetchImpl ?? globalThis.fetch.bind(globalThis);

  async function request<T>(
    method: "GET" | "POST" | "DELETE",
    path: string,
    requestOptions: RequestOptions,
  ): Promise<ApiSuccessEnvelope<T>> {
    const headers: Record<string, string> = {};
    let body: string | undefined;
    if (requestOptions.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(requestOptions.body);
    }
    if (requestOptions.idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = requestOptions.idempotencyKey;
    }
    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      init.body = body;
    }
    if (requestOptions.signal !== undefined) {
      init.signal = requestOptions.signal;
    }

    let response: Response;
    try {
      response = await fetchImpl(`${apiBase}${path}`, init);
    } catch (error) {
      if (isAbortError(error)) {
        throw new RequestAbortedError();
      }
      switch (requestOptions.networkFailure) {
        case "READ":
          throw new ReadNetworkError();
        case "CONVERSATION_CREATE":
          throw new ConversationCreationUncertaintyError();
        case "IDEMPOTENT_WRITE":
          throw new NetworkUncertaintyError();
      }
    }

    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      if (requestOptions.networkFailure === "CONVERSATION_CREATE") {
        throw new ConversationCreationUncertaintyError();
      }
      throw new ProtocolResponseError();
    }
    if (!response.ok) {
      if (
        response.status >= 500 &&
        requestOptions.networkFailure !== "READ"
      ) {
        if (requestOptions.networkFailure === "CONVERSATION_CREATE") {
          throw new ConversationCreationUncertaintyError();
        }
        throw new NetworkUncertaintyError();
      }
      throw parseApiError(response.status, payload);
    }
    return parseSuccess<T>(payload);
  }

  return {
    createConversation(title, idempotencyKey) {
      const body: Record<string, unknown> =
        title === undefined ? {} : { title };
      return request("POST", "/conversations", {
        body,
        idempotencyKey:
          idempotencyKey ??
          `frontend-conversation-create-${globalThis.crypto.randomUUID()}`,
        networkFailure: "CONVERSATION_CREATE",
      });
    },
    deleteConversation(conversationId) {
      return request(
        "DELETE",
        `/conversations/${encodeId(conversationId)}`,
        { networkFailure: "IDEMPOTENT_WRITE" },
      );
    },
    listConversations(limit = 20, cursor, signal) {
      return request(
        "GET",
        withQuery("/conversations", { limit, cursor }),
        signal === undefined
          ? { networkFailure: "READ" }
          : { networkFailure: "READ", signal },
      );
    },
    getToolResult(resultId, signal) {
      return request(
        "GET",
        `/tool-results/${encodeId(resultId)}`,
        signal === undefined
          ? { networkFailure: "READ" }
          : { networkFailure: "READ", signal },
      );
    },
    getAssetMetadata(assetId, signal) {
      return request(
        "GET",
        `/assets/${encodeId(assetId)}`,
        signal === undefined
          ? { networkFailure: "READ" }
          : { networkFailure: "READ", signal },
      );
    },
    resolvePublicContentUrl(contentUrl) {
      return resolvePublicContentUrlAgainstBase(contentUrl, apiBase);
    },
  };
}
