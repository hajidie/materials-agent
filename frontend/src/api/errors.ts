import type {
  ApiErrorDetail,
  ApiResourceReference,
} from "./types";

export class ApiResponseError extends Error {
  readonly kind = "API_RESPONSE_ERROR";

  constructor(
    readonly status: number,
    readonly requestId: string,
    readonly code: string,
    message: string,
    readonly details: ApiErrorDetail[],
    readonly resource: ApiResourceReference,
  ) {
    super(message);
    this.name = "ApiResponseError";
  }
}

export class NetworkUncertaintyError extends Error {
  readonly kind = "NETWORK_UNCERTAINTY";

  constructor() {
    super("未能确认请求结果。可以使用原幂等键重试。");
    this.name = "NetworkUncertaintyError";
  }
}

export class ReadNetworkError extends Error {
  readonly kind = "READ_NETWORK_ERROR";

  constructor() {
    super("读取失败，请检查网络后重试。");
    this.name = "ReadNetworkError";
  }
}

export class ConversationCreationUncertaintyError extends Error {
  readonly kind = "CONVERSATION_CREATION_UNCERTAINTY";

  constructor() {
    super(
      "无法确认 Conversation 是否已创建；请使用原幂等键重试，避免重复创建。",
    );
    this.name = "ConversationCreationUncertaintyError";
  }
}

export class ProtocolResponseError extends Error {
  readonly kind = "PROTOCOL_RESPONSE_ERROR";

  constructor(_reason?: string) {
    super("服务返回了无法识别的响应。");
    this.name = "ProtocolResponseError";
  }
}

export class RequestAbortedError extends Error {
  readonly kind = "REQUEST_ABORTED";

  constructor() {
    super("读取请求已取消。");
    this.name = "RequestAbortedError";
  }
}

export type ClientError =
  | ApiResponseError
  | NetworkUncertaintyError
  | ReadNetworkError
  | ConversationCreationUncertaintyError
  | ProtocolResponseError
  | RequestAbortedError;

export interface UserVisibleError {
  message: string;
  status?: number;
  request_id?: string;
  details?: ApiErrorDetail[];
}

export function toUserVisibleError(error: unknown): UserVisibleError {
  if (error instanceof ApiResponseError) {
    return {
      message: error.message,
      status: error.status,
      request_id: error.requestId,
      details: error.details,
    };
  }
  if (error instanceof NetworkUncertaintyError) {
    return { message: "未能确认请求结果。可以使用原幂等键重试。" };
  }
  if (error instanceof ReadNetworkError) {
    return { message: "读取失败，请检查网络后重试。" };
  }
  if (error instanceof ConversationCreationUncertaintyError) {
    return {
      message:
        "无法确认 Conversation 是否已创建；请使用原幂等键重试，避免重复创建。",
    };
  }
  if (error instanceof ProtocolResponseError) {
    return { message: "服务返回了无法识别的响应。" };
  }
  if (error instanceof RequestAbortedError) {
    return { message: "读取请求已取消。" };
  }
  return { message: "请求处理失败。" };
}
