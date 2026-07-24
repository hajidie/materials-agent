import { readonly, ref, type DeepReadonly, type Ref } from "vue";

import {
  ApiResponseError,
  NetworkUncertaintyError,
  ProtocolResponseError,
} from "../api/errors";
import type {
  ExplanationRetryRequest,
  MessageSubmissionRequest,
  ToolRetryRequest,
} from "../api/types";

export type MutationOperation =
  | "TASK_CREATE"
  | "TASK_INPUT_SUPPLEMENT"
  | "TOOL_RETRY"
  | "EXPLANATION_RETRY";

export type MutationStatus =
  | "IDLE"
  | "SENDING"
  | "SUCCEEDED"
  | "UNCERTAIN"
  | "BUSINESS_FAILED";

interface PendingMutationBase {
  version: 1;
  resourceId: string;
  idempotencyKey: string;
  createdAt: string;
}

export interface NewTaskMutationBody
  extends MessageSubmissionRequest {
  submission_mode: "NEW_TASK";
  target_task_id: null;
}

export interface SupplementMutationBody
  extends MessageSubmissionRequest {
  submission_mode: "SUPPLEMENT_TASK";
  target_task_id: string;
}

export type MutationRequestBody =
  | NewTaskMutationBody
  | SupplementMutationBody
  | ToolRetryRequest
  | ExplanationRetryRequest;

export type PendingMutationV1 =
  | (PendingMutationBase & {
      operation: "TASK_CREATE";
      body: NewTaskMutationBody;
    })
  | (PendingMutationBase & {
      operation: "TASK_INPUT_SUPPLEMENT";
      body: SupplementMutationBody;
    })
  | (PendingMutationBase & {
      operation: "TOOL_RETRY";
      body: ToolRetryRequest;
    })
  | (PendingMutationBase & {
      operation: "EXPLANATION_RETRY";
      body: ExplanationRetryRequest;
    });

export type MutationSender = (
  descriptor: PendingMutationV1,
) => Promise<unknown>;

export interface UseIdempotentRequestOptions {
  send: MutationSender;
  storage?: Storage;
  keyFactory?: () => string;
  now?: () => string;
}

export interface IdempotentRequestManager {
  status: Readonly<Ref<MutationStatus>>;
  pending: DeepReadonly<Ref<PendingMutationV1 | null>>;
  start<T>(
    operation: MutationOperation,
    resourceId: string,
    body: MutationRequestBody,
  ): Promise<T>;
  retryPending<T>(): Promise<T>;
  discardPending(): { discarded: boolean };
}

export const PENDING_MUTATION_STORAGE_KEY =
  "materialsagent:m10:pending-mutation:v1";

const KEY_PREFIXES: Record<MutationOperation, string> = {
  TASK_CREATE: "frontend-task-create",
  TASK_INPUT_SUPPLEMENT: "frontend-task-input-supplement",
  TOOL_RETRY: "frontend-tool-retry",
  EXPLANATION_RETRY: "frontend-explanation-retry",
};

function defaultStorage(): Storage {
  return globalThis.sessionStorage;
}

function defaultKeyFactory(): string {
  return globalThis.crypto.randomUUID();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}

function isMutationOperation(
  value: unknown,
): value is MutationOperation {
  return (
    value === "TASK_CREATE" ||
    value === "TASK_INPUT_SUPPLEMENT" ||
    value === "TOOL_RETRY" ||
    value === "EXPLANATION_RETRY"
  );
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  const actual = Object.keys(value);
  return (
    actual.length === keys.length &&
    actual.every((key) => keys.includes(key))
  );
}

function isNewTaskBody(
  value: unknown,
): value is NewTaskMutationBody {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "submission_mode",
      "content_text",
      "target_task_id",
    ]) &&
    value.submission_mode === "NEW_TASK" &&
    typeof value.content_text === "string" &&
    value.content_text.trim().length > 0 &&
    value.target_task_id === null
  );
}

function isSupplementBody(
  value: unknown,
): value is SupplementMutationBody {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "submission_mode",
      "content_text",
      "target_task_id",
    ]) &&
    value.submission_mode === "SUPPLEMENT_TASK" &&
    typeof value.content_text === "string" &&
    value.content_text.trim().length > 0 &&
    typeof value.target_task_id === "string" &&
    value.target_task_id.length > 0
  );
}

function isToolRetryBody(
  value: unknown,
): value is ToolRetryRequest {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["reason"]) &&
    value.reason === "USER_REQUESTED_RETRY"
  );
}

function isExplanationRetryBody(
  value: unknown,
): value is ExplanationRetryRequest {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["language", "reason"]) &&
    typeof value.language === "string" &&
    value.language.length > 0 &&
    value.reason === "USER_REQUESTED_RETRY"
  );
}

function isValidIdempotencyKey(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 255 &&
    !/\p{Cc}/u.test(value)
  );
}

function parsePendingMutation(value: string): PendingMutationV1 | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return null;
  }
  if (
    !isRecord(parsed) ||
    parsed.version !== 1 ||
    !isMutationOperation(parsed.operation) ||
    typeof parsed.resourceId !== "string" ||
    parsed.resourceId.length === 0 ||
    !isValidIdempotencyKey(parsed.idempotencyKey) ||
    typeof parsed.createdAt !== "string" ||
    !Number.isFinite(Date.parse(parsed.createdAt))
  ) {
    return null;
  }
  const common = {
    version: 1,
    resourceId: parsed.resourceId,
    idempotencyKey: parsed.idempotencyKey,
    createdAt: parsed.createdAt,
  } as const;
  switch (parsed.operation) {
    case "TASK_CREATE":
      return isNewTaskBody(parsed.body)
        ? {
            ...common,
            operation: "TASK_CREATE",
            body: { ...parsed.body },
          }
        : null;
    case "TASK_INPUT_SUPPLEMENT":
      return isSupplementBody(parsed.body)
        ? {
            ...common,
            operation: "TASK_INPUT_SUPPLEMENT",
            body: { ...parsed.body },
          }
        : null;
    case "TOOL_RETRY":
      return isToolRetryBody(parsed.body)
        ? {
            ...common,
            operation: "TOOL_RETRY",
            body: { ...parsed.body },
          }
        : null;
    case "EXPLANATION_RETRY":
      return isExplanationRetryBody(parsed.body)
        ? {
            ...common,
            operation: "EXPLANATION_RETRY",
            body: { ...parsed.body },
          }
        : null;
  }
}

function createPendingMutation(
  operation: MutationOperation,
  resourceId: string,
  body: MutationRequestBody,
  idempotencyKey: string,
  createdAt: string,
): PendingMutationV1 {
  const common = {
    version: 1,
    resourceId,
    idempotencyKey,
    createdAt,
  } as const;
  switch (operation) {
    case "TASK_CREATE":
      if (isNewTaskBody(body)) {
        return {
          ...common,
          operation: "TASK_CREATE",
          body: { ...body },
        };
      }
      break;
    case "TASK_INPUT_SUPPLEMENT":
      if (isSupplementBody(body)) {
        return {
          ...common,
          operation: "TASK_INPUT_SUPPLEMENT",
          body: { ...body },
        };
      }
      break;
    case "TOOL_RETRY":
      if (isToolRetryBody(body)) {
        return {
          ...common,
          operation: "TOOL_RETRY",
          body: { ...body },
        };
      }
      break;
    case "EXPLANATION_RETRY":
      if (isExplanationRetryBody(body)) {
        return {
          ...common,
          operation: "EXPLANATION_RETRY",
          body: { ...body },
        };
      }
      break;
  }
  throw new Error("写操作描述符与 operation 不匹配。");
}

export function useIdempotentRequest(
  options: UseIdempotentRequestOptions,
): IdempotentRequestManager {
  const storage = options.storage ?? defaultStorage();
  const keyFactory = options.keyFactory ?? defaultKeyFactory;
  const now = options.now ?? (() => new Date().toISOString());
  const status = ref<MutationStatus>("IDLE");
  const pending = ref<PendingMutationV1 | null>(null);

  const stored = storage.getItem(PENDING_MUTATION_STORAGE_KEY);
  if (stored !== null) {
    const restored = parsePendingMutation(stored);
    if (restored === null) {
      storage.removeItem(PENDING_MUTATION_STORAGE_KEY);
    } else {
      pending.value = restored;
      status.value = "UNCERTAIN";
    }
  }

  function clearPending(): void {
    storage.removeItem(PENDING_MUTATION_STORAGE_KEY);
    pending.value = null;
  }

  async function sendPending<T>(
    descriptor: PendingMutationV1,
  ): Promise<T> {
    status.value = "SENDING";
    try {
      const result = await options.send(descriptor);
      clearPending();
      status.value = "SUCCEEDED";
      return result as T;
    } catch (error) {
      if (error instanceof ApiResponseError) {
        clearPending();
        status.value = "BUSINESS_FAILED";
      } else if (
        error instanceof NetworkUncertaintyError ||
        error instanceof ProtocolResponseError
      ) {
        status.value = "UNCERTAIN";
      } else {
        status.value = "UNCERTAIN";
      }
      throw error;
    }
  }

  async function start<T>(
    operation: MutationOperation,
    resourceId: string,
    body: MutationRequestBody,
  ): Promise<T> {
    if (status.value === "SENDING") {
      throw new Error("写操作正在处理中，请等待当前请求完成。");
    }
    if (status.value === "UNCERTAIN") {
      throw new Error("存在结果待确认的写操作，请先重试或放弃。");
    }

    const idempotencyKey =
      `${KEY_PREFIXES[operation]}-${keyFactory()}`;
    if (!isValidIdempotencyKey(idempotencyKey)) {
      throw new Error("无法创建符合限制的幂等请求。");
    }
    const descriptor = createPendingMutation(
      operation,
      resourceId,
      body,
      idempotencyKey,
      now(),
    );
    storage.setItem(
      PENDING_MUTATION_STORAGE_KEY,
      JSON.stringify(descriptor),
    );
    pending.value = descriptor;
    return sendPending<T>(descriptor);
  }

  async function retryPending<T>(): Promise<T> {
    if (status.value === "SENDING") {
      throw new Error("写操作正在处理中，请等待当前请求完成。");
    }
    const descriptor = pending.value;
    if (descriptor === null) {
      throw new Error("没有可重试的待确认写操作。");
    }
    return sendPending<T>(descriptor);
  }

  function discardPending(): { discarded: boolean } {
    if (status.value === "SENDING") {
      return { discarded: false };
    }
    const discarded = pending.value !== null;
    clearPending();
    status.value = "IDLE";
    return { discarded };
  }

  return {
    status: readonly(status),
    pending: readonly(pending),
    start,
    retryPending,
    discardPending,
  };
}
