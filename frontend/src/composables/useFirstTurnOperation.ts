import { readonly, ref, type DeepReadonly, type Ref } from "vue";

import type { MaterialsAgentApi } from "../api/client";
import { ApiResponseError } from "../api/errors";
import type { ApiSuccessEnvelope, MessageSubmissionResponseData } from "../api/types";
import type { MutationStatus, NewTaskMutationBody } from "./useIdempotentRequest";


export const FIRST_TURN_STORAGE_KEY =
  "materialsagent:first-turn-operation:v1";

export type FirstTurnStage =
  | "CREATE_CONVERSATION"
  | "SUBMIT_MESSAGE";

export interface FirstTurnDescriptorV1 {
  version: 1;
  operation: "FIRST_TURN";
  clientOperationId: string;
  originalDraft: string;
  stage: FirstTurnStage;
  conversationKey: string;
  messageKey: string;
  conversationId: string | null;
  createdAt: string;
}

export interface FirstTurnManager {
  pending: DeepReadonly<Ref<FirstTurnDescriptorV1 | null>>;
  status: Readonly<Ref<MutationStatus>>;
  start(contentText: string): Promise<ApiSuccessEnvelope<MessageSubmissionResponseData>>;
  retry(): Promise<ApiSuccessEnvelope<MessageSubmissionResponseData>>;
}

interface FirstTurnOptions {
  api: MaterialsAgentApi;
  storage?: Storage;
  keyFactory?: () => string;
  now?: () => string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validKey(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 255 &&
    !/\p{Cc}/u.test(value)
  );
}

function parseDescriptor(raw: string): FirstTurnDescriptorV1 | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (
    !isRecord(value) ||
    Object.keys(value).length !== 9 ||
    value.version !== 1 ||
    value.operation !== "FIRST_TURN" ||
    typeof value.clientOperationId !== "string" ||
    !value.clientOperationId ||
    typeof value.originalDraft !== "string" ||
    !value.originalDraft.trim() ||
    (value.stage !== "CREATE_CONVERSATION" &&
      value.stage !== "SUBMIT_MESSAGE") ||
    !validKey(value.conversationKey) ||
    !validKey(value.messageKey) ||
    (value.conversationId !== null &&
      (typeof value.conversationId !== "string" || !value.conversationId)) ||
    typeof value.createdAt !== "string" ||
    !Number.isFinite(Date.parse(value.createdAt)) ||
    (value.stage === "SUBMIT_MESSAGE" && value.conversationId === null)
  ) {
    return null;
  }
  return {
    version: 1,
    operation: "FIRST_TURN",
    clientOperationId: value.clientOperationId,
    originalDraft: value.originalDraft,
    stage: value.stage,
    conversationKey: value.conversationKey,
    messageKey: value.messageKey,
    conversationId: value.conversationId,
    createdAt: value.createdAt,
  };
}

export function useFirstTurnOperation(
  options: FirstTurnOptions,
): FirstTurnManager {
  const storage = options.storage ?? globalThis.sessionStorage;
  const keyFactory = options.keyFactory ?? (() => globalThis.crypto.randomUUID());
  const now = options.now ?? (() => new Date().toISOString());
  const pending = ref<FirstTurnDescriptorV1 | null>(null);
  const status = ref<MutationStatus>("IDLE");

  const stored = storage.getItem(FIRST_TURN_STORAGE_KEY);
  if (stored !== null) {
    const restored = parseDescriptor(stored);
    if (restored === null) {
      storage.removeItem(FIRST_TURN_STORAGE_KEY);
    } else {
      pending.value = restored;
      status.value = "UNCERTAIN";
    }
  }

  function persist(descriptor: FirstTurnDescriptorV1): void {
    storage.setItem(FIRST_TURN_STORAGE_KEY, JSON.stringify(descriptor));
    pending.value = descriptor;
  }

  function clear(): void {
    storage.removeItem(FIRST_TURN_STORAGE_KEY);
    pending.value = null;
  }

  async function execute(
    initial: FirstTurnDescriptorV1,
  ): Promise<ApiSuccessEnvelope<MessageSubmissionResponseData>> {
    status.value = "SENDING";
    let descriptor = initial;
    try {
      if (descriptor.stage === "CREATE_CONVERSATION") {
        const created = await options.api.createConversation(
          undefined,
          descriptor.conversationKey,
        );
        descriptor = {
          ...descriptor,
          stage: "SUBMIT_MESSAGE",
          conversationId: created.data.conversation_id,
        };
        persist(descriptor);
      }
      if (descriptor.conversationId === null) {
        throw new Error("首条消息恢复记录缺少 Conversation 身份。");
      }
      const body: NewTaskMutationBody = {
        submission_mode: "NEW_TASK",
        content_text: descriptor.originalDraft,
        target_task_id: null,
      };
      const response = await options.api.submitMessage(
        descriptor.conversationId,
        body,
        descriptor.messageKey,
      );
      clear();
      status.value = "SUCCEEDED";
      return response;
    } catch (error) {
      status.value =
        error instanceof ApiResponseError && error.status < 500
          ? "BUSINESS_FAILED"
          : "UNCERTAIN";
      throw error;
    }
  }

  async function start(
    contentText: string,
  ): Promise<ApiSuccessEnvelope<MessageSubmissionResponseData>> {
    if (pending.value !== null || status.value === "SENDING") {
      throw new Error("首条消息尚未完整确认，请继续重试原请求。");
    }
    const draft = contentText.trim();
    if (!draft) {
      throw new Error("请输入消息后再发送。");
    }
    const clientOperationId = keyFactory();
    const descriptor: FirstTurnDescriptorV1 = {
      version: 1,
      operation: "FIRST_TURN",
      clientOperationId,
      originalDraft: draft,
      stage: "CREATE_CONVERSATION",
      conversationKey: `frontend-conversation-create-${clientOperationId}`,
      messageKey: `frontend-task-create-${clientOperationId}`,
      conversationId: null,
      createdAt: now(),
    };
    if (!validKey(descriptor.conversationKey) || !validKey(descriptor.messageKey)) {
      throw new Error("无法创建符合限制的首条消息幂等请求。");
    }
    persist(descriptor);
    return execute(descriptor);
  }

  async function retry(): Promise<ApiSuccessEnvelope<MessageSubmissionResponseData>> {
    if (status.value === "SENDING") {
      throw new Error("首条消息正在处理中，请等待当前请求完成。");
    }
    if (pending.value === null) {
      throw new Error("没有可重试的首条消息。");
    }
    return execute(pending.value);
  }

  return {
    pending: readonly(pending),
    status: readonly(status),
    start,
    retry,
  };
}
