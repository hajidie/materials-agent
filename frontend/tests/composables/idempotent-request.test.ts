import { describe, expect, it, vi } from "vitest";

import {
  ApiResponseError,
  NetworkUncertaintyError,
  ProtocolResponseError,
} from "../../src/api/errors";
import {
  PENDING_MUTATION_STORAGE_KEY,
  useIdempotentRequest,
  type MutationSender,
  type MutationOperation,
  type MutationRequestBody,
  type PendingMutationV1,
} from "../../src/composables/useIdempotentRequest";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

const emptyResource = {
  conversation_id: null,
  task_id: null,
  tool_run_id: null,
  result_id: null,
};

function apiError(status: number): ApiResponseError {
  return new ApiResponseError(
    status,
    `request-${status}`,
    `STATUS_${status}`,
    "明确的业务失败",
    [],
    emptyResource,
  );
}

function createSubject(
  send: MutationSender,
  storage = new MemoryStorage(),
  keyFactory = vi.fn(() => "fixed-uuid"),
) {
  return {
    manager: useIdempotentRequest({
      send,
      storage,
      keyFactory,
      now: () => "2026-07-24T00:00:00.000Z",
    }),
    storage,
    keyFactory,
  };
}

function validBody(
  operation: MutationOperation,
): MutationRequestBody {
  switch (operation) {
    case "TASK_CREATE":
      return {
        submission_mode: "NEW_TASK",
        content_text: "create",
        target_task_id: null,
      };
    case "TASK_INPUT_SUPPLEMENT":
      return {
        submission_mode: "SUPPLEMENT_TASK",
        content_text: "more context",
        target_task_id: "task-1",
      };
    case "TOOL_RETRY":
      return { reason: "USER_REQUESTED_RETRY" };
    case "EXPLANATION_RETRY":
      return {
        language: "zh-CN",
        reason: "USER_REQUESTED_RETRY",
      };
  }
}

describe("useIdempotentRequest", () => {
  it("persists the descriptor before sending", async () => {
    const storage = new MemoryStorage();
    const send = vi.fn((descriptor: PendingMutationV1) => {
      expect(JSON.parse(storage.getItem(PENDING_MUTATION_STORAGE_KEY)!))
        .toEqual(descriptor);
      return Promise.resolve({ accepted: true });
    });
    const { manager } = createSubject(send, storage);

    await manager.start(
      "TASK_CREATE",
      "conversation-1",
      validBody("TASK_CREATE"),
    );

    expect(send).toHaveBeenCalledOnce();
  });

  it.each([
    ["TASK_CREATE", "frontend-task-create-fixed-uuid"],
    [
      "TASK_INPUT_SUPPLEMENT",
      "frontend-task-input-supplement-fixed-uuid",
    ],
    ["TOOL_RETRY", "frontend-tool-retry-fixed-uuid"],
    [
      "EXPLANATION_RETRY",
      "frontend-explanation-retry-fixed-uuid",
    ],
  ] as const)("generates a valid opaque key for %s", async (
    operation,
    expected,
  ) => {
    const send = vi.fn<MutationSender>(() =>
      Promise.resolve({ accepted: true }),
    );
    const { manager } = createSubject(send);

    await manager.start(operation, "resource-1", validBody(operation));

    const descriptor = send.mock.calls[0]?.[0];
    expect(descriptor?.idempotencyKey).toBe(expected);
    expect(descriptor?.idempotencyKey.length).toBeLessThanOrEqual(255);
  });

  it("clears the pending record after an explicit success", async () => {
    const { manager, storage } = createSubject(
      vi.fn(() => Promise.resolve({ accepted: true })),
    );

    await expect(
      manager.start(
        "TASK_CREATE",
        "conversation-1",
        validBody("TASK_CREATE"),
      ),
    ).resolves.toEqual({ accepted: true });

    expect(manager.status.value).toBe("SUCCEEDED");
    expect(manager.pending.value).toBeNull();
    expect(storage.getItem(PENDING_MUTATION_STORAGE_KEY)).toBeNull();
  });

  it.each([409, 422, 503])(
    "clears the pending record after an explicit %s API response",
    async (status) => {
      const error = apiError(status);
      const { manager, storage } = createSubject(
        vi.fn(() => Promise.reject(error)),
      );

      await expect(
        manager.start(
          "TASK_CREATE",
          "conversation-1",
          validBody("TASK_CREATE"),
        ),
      ).rejects.toBe(error);

      expect(manager.status.value).toBe("BUSINESS_FAILED");
      expect(manager.pending.value).toBeNull();
      expect(storage.getItem(PENDING_MUTATION_STORAGE_KEY)).toBeNull();
    },
  );

  it("retains the pending record after a network uncertainty", async () => {
    const error = new NetworkUncertaintyError();
    const { manager, storage } = createSubject(
      vi.fn(() => Promise.reject(error)),
    );

    await expect(
      manager.start(
        "TOOL_RETRY",
        "task-1",
        validBody("TOOL_RETRY"),
      ),
    ).rejects.toBe(error);

    expect(manager.status.value).toBe("UNCERTAIN");
    expect(manager.pending.value?.operation).toBe("TOOL_RETRY");
    expect(storage.getItem(PENDING_MUTATION_STORAGE_KEY)).not.toBeNull();
  });

  it("retains the pending record after a protocol uncertainty", async () => {
    const error = new ProtocolResponseError();
    const { manager, storage } = createSubject(
      vi.fn(() => Promise.reject(error)),
    );

    await expect(
      manager.start(
        "EXPLANATION_RETRY",
        "result-1",
        validBody("EXPLANATION_RETRY"),
      ),
    ).rejects.toBe(error);

    expect(manager.status.value).toBe("UNCERTAIN");
    expect(storage.getItem(PENDING_MUTATION_STORAGE_KEY)).not.toBeNull();
  });

  it("retryPending reuses the original key and body", async () => {
    const send = vi
      .fn<MutationSender>()
      .mockRejectedValueOnce(new NetworkUncertaintyError())
      .mockResolvedValueOnce({ accepted: true });
    const { manager, keyFactory } = createSubject(send);
    const body = validBody("TASK_INPUT_SUPPLEMENT");

    await expect(
      manager.start(
        "TASK_INPUT_SUPPLEMENT",
        "conversation-1",
        body,
      ),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);
    const original = send.mock.calls[0]?.[0];

    await manager.retryPending();

    const retried = send.mock.calls[1]?.[0];
    expect(retried?.idempotencyKey).toBe(original?.idempotencyKey);
    expect(retried?.body).toEqual(original?.body);
    expect(keyFactory).toHaveBeenCalledOnce();
  });

  it("generates a new key only when the user starts a new operation", async () => {
    const keyFactory = vi
      .fn()
      .mockReturnValueOnce("uuid-one")
      .mockReturnValueOnce("uuid-two");
    const send = vi.fn<MutationSender>(() =>
      Promise.resolve({ accepted: true }),
    );
    const { manager } = createSubject(
      send,
      new MemoryStorage(),
      keyFactory,
    );

    await manager.start(
      "TASK_CREATE",
      "conversation-1",
      validBody("TASK_CREATE"),
    );
    await manager.start(
      "TOOL_RETRY",
      "task-1",
      validBody("TOOL_RETRY"),
    );

    expect(send.mock.calls[0]?.[0].idempotencyKey).toContain("uuid-one");
    expect(send.mock.calls[1]?.[0].idempotencyKey).toContain("uuid-two");
    expect(keyFactory).toHaveBeenCalledTimes(2);
  });

  it("rejects a second start while a request is sending", async () => {
    let resolveSend: ((value: unknown) => void) | undefined;
    const send = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSend = resolve;
        }),
    );
    const { manager, keyFactory } = createSubject(send);

    const first = manager.start(
      "TASK_CREATE",
      "conversation-1",
      validBody("TASK_CREATE"),
    );
    await expect(
      manager.start(
        "TOOL_RETRY",
        "task-1",
        validBody("TOOL_RETRY"),
      ),
    ).rejects.toThrow("写操作正在处理中");
    expect(send).toHaveBeenCalledOnce();
    expect(keyFactory).toHaveBeenCalledOnce();
    resolveSend?.({ accepted: true });
    await first;
  });

  it("rejects a new operation while an uncertain request exists", async () => {
    const send = vi.fn(() =>
      Promise.reject(new NetworkUncertaintyError()),
    );
    const { manager, keyFactory } = createSubject(send);
    await expect(
      manager.start(
        "TASK_CREATE",
        "conversation-1",
        validBody("TASK_CREATE"),
      ),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);

    await expect(
      manager.start(
        "TOOL_RETRY",
        "task-1",
        validBody("TOOL_RETRY"),
      ),
    ).rejects.toThrow("待确认");
    expect(send).toHaveBeenCalledOnce();
    expect(keyFactory).toHaveBeenCalledOnce();
  });

  it("restores a valid stored descriptor as uncertain without replaying", () => {
    const storage = new MemoryStorage();
    const descriptor: PendingMutationV1 = {
      version: 1,
      operation: "TASK_CREATE",
      resourceId: "conversation-1",
      body: {
        submission_mode: "NEW_TASK",
        content_text: "hello",
        target_task_id: null,
      },
      idempotencyKey: "frontend-task-create-stored",
      createdAt: "2026-07-24T00:00:00.000Z",
    };
    storage.setItem(
      PENDING_MUTATION_STORAGE_KEY,
      JSON.stringify(descriptor),
    );
    const send = vi.fn(() => Promise.resolve({ accepted: true }));

    const { manager } = createSubject(send, storage);

    expect(manager.status.value).toBe("UNCERTAIN");
    expect(manager.pending.value).toEqual(descriptor);
    expect(send).not.toHaveBeenCalled();
  });

  it.each([
    "{bad json",
    JSON.stringify({ version: 2 }),
    JSON.stringify({
      version: 1,
      operation: "TASK_CREATE",
      resourceId: "",
      body: {},
      idempotencyKey: "key",
      createdAt: "not-a-date",
    }),
  ])("safely clears corrupt storage without replaying", (stored) => {
    const storage = new MemoryStorage();
    storage.setItem(PENDING_MUTATION_STORAGE_KEY, stored);
    const send = vi.fn(() => Promise.resolve({ accepted: true }));

    const { manager } = createSubject(send, storage);

    expect(manager.status.value).toBe("IDLE");
    expect(manager.pending.value).toBeNull();
    expect(storage.getItem(PENDING_MUTATION_STORAGE_KEY)).toBeNull();
    expect(send).not.toHaveBeenCalled();
  });

  it.each([
    [
      "TASK_CREATE",
      {
        submission_mode: "SUPPLEMENT_TASK",
        content_text: "wrong mode",
        target_task_id: "task-1",
      },
    ],
    [
      "TASK_INPUT_SUPPLEMENT",
      {
        submission_mode: "SUPPLEMENT_TASK",
        content_text: "missing target",
        target_task_id: null,
      },
    ],
    ["TOOL_RETRY", { language: "zh-CN" }],
    [
      "EXPLANATION_RETRY",
      { language: "zh-CN", reason: 123 },
    ],
  ] as const)(
    "clears stored %s descriptor with an operation-mismatched body",
    (operation, body) => {
      const storage = new MemoryStorage();
      storage.setItem(
        PENDING_MUTATION_STORAGE_KEY,
        JSON.stringify({
          version: 1,
          operation,
          resourceId: "resource-1",
          body,
          idempotencyKey: "frontend-stored-key",
          createdAt: "2026-07-24T00:00:00.000Z",
        }),
      );
      const send = vi.fn(() => Promise.resolve({ accepted: true }));

      const { manager } = createSubject(send, storage);

      expect(manager.status.value).toBe("IDLE");
      expect(manager.pending.value).toBeNull();
      expect(storage.getItem(PENDING_MUTATION_STORAGE_KEY)).toBeNull();
      expect(send).not.toHaveBeenCalled();
    },
  );

  it("accepts a stored Idempotency-Key of exactly 255 characters", () => {
    const storage = new MemoryStorage();
    const idempotencyKey = "k".repeat(255);
    storage.setItem(
      PENDING_MUTATION_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        operation: "TASK_CREATE",
        resourceId: "conversation-1",
        body: validBody("TASK_CREATE"),
        idempotencyKey,
        createdAt: "2026-07-24T00:00:00.000Z",
      }),
    );

    const { manager } = createSubject(
      vi.fn(() => Promise.resolve({ accepted: true })),
      storage,
    );

    expect(manager.status.value).toBe("UNCERTAIN");
    expect(manager.pending.value?.idempotencyKey).toBe(idempotencyKey);
  });

  it("generates an Idempotency-Key of exactly 255 characters", async () => {
    const prefix = "frontend-task-create-";
    const keyFactory = vi.fn(() => "k".repeat(255 - prefix.length));
    const send = vi.fn<MutationSender>(() =>
      Promise.resolve({ accepted: true }),
    );
    const { manager } = createSubject(
      send,
      new MemoryStorage(),
      keyFactory,
    );

    await manager.start(
      "TASK_CREATE",
      "conversation-1",
      validBody("TASK_CREATE"),
    );

    expect(send.mock.calls[0]?.[0].idempotencyKey).toHaveLength(255);
  });

  it("clears a stored key containing a Unicode control character", () => {
    const storage = new MemoryStorage();
    storage.setItem(
      PENDING_MUTATION_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        operation: "TOOL_RETRY",
        resourceId: "task-1",
        body: validBody("TOOL_RETRY"),
        idempotencyKey: "frontend-tool-retry-\u0000unsafe",
        createdAt: "2026-07-24T00:00:00.000Z",
      }),
    );
    const send = vi.fn(() => Promise.resolve({ accepted: true }));

    const { manager } = createSubject(send, storage);

    expect(manager.status.value).toBe("IDLE");
    expect(manager.pending.value).toBeNull();
    expect(storage.getItem(PENDING_MUTATION_STORAGE_KEY)).toBeNull();
    expect(send).not.toHaveBeenCalled();
  });

  it("discardPending clears locally without sending", async () => {
    const send = vi.fn(() =>
      Promise.reject(new NetworkUncertaintyError()),
    );
    const { manager, storage } = createSubject(send);
    await expect(
      manager.start(
        "TASK_CREATE",
        "conversation-1",
        validBody("TASK_CREATE"),
      ),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);

    expect(manager.discardPending()).toEqual({ discarded: true });

    expect(manager.status.value).toBe("IDLE");
    expect(manager.pending.value).toBeNull();
    expect(storage.getItem(PENDING_MUTATION_STORAGE_KEY)).toBeNull();
    expect(send).toHaveBeenCalledOnce();
  });

  it("does not automatically retry uncertain operations", async () => {
    vi.useFakeTimers();
    try {
      const send = vi.fn(() =>
        Promise.reject(new NetworkUncertaintyError()),
      );
      const { manager } = createSubject(send);

      await expect(
        manager.start(
          "TASK_CREATE",
          "conversation-1",
          validBody("TASK_CREATE"),
        ),
      ).rejects.toBeInstanceOf(NetworkUncertaintyError);
      await vi.advanceTimersByTimeAsync(60_000);

      expect(send).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });
});
