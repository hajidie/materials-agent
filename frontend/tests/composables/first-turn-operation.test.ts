import { describe, expect, it, vi } from "vitest";

import type { MaterialsAgentApi } from "../../src/api/client";
import {
  ConversationCreationUncertaintyError,
  NetworkUncertaintyError,
} from "../../src/api/errors";
import type { MessageSubmissionResponseData } from "../../src/api/types";
import {
  FIRST_TURN_STORAGE_KEY,
  useFirstTurnOperation,
} from "../../src/composables/useFirstTurnOperation";


class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();
  get length(): number { return this.values.size; }
  clear(): void { this.values.clear(); }
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string): void { this.values.delete(key); }
  setItem(key: string, value: string): void { this.values.set(key, value); }
}

const messageData = {
  conversation_id: "conv-stable",
} as MessageSubmissionResponseData;

function api(
  createConversation: MaterialsAgentApi["createConversation"],
  submitMessage: MaterialsAgentApi["submitMessage"],
): MaterialsAgentApi {
  return {
    createConversation,
    submitMessage,
  } as MaterialsAgentApi;
}

describe("useFirstTurnOperation", () => {
  it("replays uncertain Conversation creation with the same derived key", async () => {
    const storage = new MemoryStorage();
    const create = vi.fn<MaterialsAgentApi["createConversation"]>()
      .mockRejectedValueOnce(new ConversationCreationUncertaintyError())
      .mockResolvedValue({
        request_id: "req-create",
        data: {
          conversation_id: "conv-stable",
          title: null,
          created_at: "2026-09-03T00:00:00Z",
          updated_at: "2026-09-03T00:00:00Z",
        },
      });
    const submit = vi.fn<MaterialsAgentApi["submitMessage"]>()
      .mockResolvedValue({ request_id: "req-message", data: messageData });
    const manager = useFirstTurnOperation({
      api: api(create, submit),
      storage,
      keyFactory: () => "operation-stable",
      now: () => "2026-09-03T00:00:00Z",
    });

    await expect(manager.start("original draft")).rejects.toBeInstanceOf(
      ConversationCreationUncertaintyError,
    );
    expect(manager.pending.value?.stage).toBe("CREATE_CONVERSATION");
    expect(storage.getItem(FIRST_TURN_STORAGE_KEY)).not.toBeNull();

    await manager.retry();

    expect(create.mock.calls).toEqual([
      [undefined, "frontend-conversation-create-operation-stable"],
      [undefined, "frontend-conversation-create-operation-stable"],
    ]);
    expect(submit).toHaveBeenCalledWith(
      "conv-stable",
      {
        submission_mode: "NEW_TASK",
        content_text: "original draft",
        target_task_id: null,
      },
      "frontend-task-create-operation-stable",
    );
    expect(manager.pending.value).toBeNull();
    expect(storage.getItem(FIRST_TURN_STORAGE_KEY)).toBeNull();
  });

  it("persists the confirmed Conversation before retrying only the Message", async () => {
    const storage = new MemoryStorage();
    const create = vi.fn<MaterialsAgentApi["createConversation"]>()
      .mockResolvedValue({
        request_id: "req-create",
        data: {
          conversation_id: "conv-stable",
          title: null,
          created_at: "2026-09-03T00:00:00Z",
          updated_at: "2026-09-03T00:00:00Z",
        },
      });
    const submit = vi.fn<MaterialsAgentApi["submitMessage"]>()
      .mockRejectedValueOnce(new NetworkUncertaintyError())
      .mockResolvedValue({ request_id: "req-message", data: messageData });
    const manager = useFirstTurnOperation({
      api: api(create, submit),
      storage,
      keyFactory: () => "operation-stable",
      now: () => "2026-09-03T00:00:00Z",
    });

    await expect(manager.start("original draft")).rejects.toBeInstanceOf(
      NetworkUncertaintyError,
    );
    expect(manager.pending.value).toMatchObject({
      stage: "SUBMIT_MESSAGE",
      conversationId: "conv-stable",
      originalDraft: "original draft",
    });

    await manager.retry();

    expect(create).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenCalledTimes(2);
    expect(submit.mock.calls[0]?.[2]).toBe(submit.mock.calls[1]?.[2]);
    expect(manager.pending.value).toBeNull();
  });

  it("restores a persisted message-stage descriptor after refresh", async () => {
    const storage = new MemoryStorage();
    storage.setItem(
      FIRST_TURN_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        operation: "FIRST_TURN",
        clientOperationId: "operation-restored",
        originalDraft: "restored draft",
        stage: "SUBMIT_MESSAGE",
        conversationKey: "frontend-conversation-create-operation-restored",
        messageKey: "frontend-task-create-operation-restored",
        conversationId: "conv-restored",
        createdAt: "2026-09-03T00:00:00Z",
      }),
    );
    const create = vi.fn<MaterialsAgentApi["createConversation"]>();
    const submit = vi.fn<MaterialsAgentApi["submitMessage"]>()
      .mockResolvedValue({ request_id: "req-message", data: messageData });

    const manager = useFirstTurnOperation({
      api: api(create, submit),
      storage,
    });

    expect(manager.status.value).toBe("UNCERTAIN");
    await manager.retry();

    expect(create).not.toHaveBeenCalled();
    expect(submit).toHaveBeenCalledWith(
      "conv-restored",
      expect.objectContaining({ content_text: "restored draft" }),
      "frontend-task-create-operation-restored",
    );
    expect(storage.getItem(FIRST_TURN_STORAGE_KEY)).toBeNull();
  });
});
