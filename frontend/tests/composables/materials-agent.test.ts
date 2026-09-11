import { afterEach, describe, expect, it, vi } from "vitest";
import { effectScope } from "vue";

import type { MaterialsAgentApi } from "../../src/api/client";
import {
  ApiResponseError,
  ConversationCreationUncertaintyError,
  NetworkUncertaintyError,
  ReadNetworkError,
} from "../../src/api/errors";
import type {
  ApiSuccessEnvelope,
  Conversation,
  ConversationListItem,
  ConversationPage,
  ExplanationRetryResponseData,
  MessageSubmissionResponseData,
  TaskDetail,
  TimelinePage,
  TimelineToolTaskItem,
  ToolRetryResponseData,
} from "../../src/api/types";
import {
  PENDING_MUTATION_STORAGE_KEY,
} from "../../src/composables/useIdempotentRequest";
import {
  useMaterialsAgent,
  type SupplementTarget,
} from "../../src/composables/useMaterialsAgent";

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

function envelope<T>(
  data: T,
  requestId = "request-1",
): ApiSuccessEnvelope<T> {
  return { request_id: requestId, data };
}

function conversation(
  id: string,
  updatedAt = "2026-07-24T00:00:00Z",
): ConversationListItem {
  return {
    conversation_id: id,
    title: id,
    created_at: "2026-07-24T00:00:00Z",
    updated_at: updatedAt,
    last_activity_preview: null,
  };
}

function conversationPage(
  items: ConversationListItem[],
  nextCursor: string | null = null,
): ConversationPage {
  return { items, next_cursor: nextCursor };
}

function toolTask(
  taskId: string,
  status: TimelineToolTaskItem["task"]["status"] = "SUCCEEDED",
  updatedAt = "2026-07-24T00:00:00Z",
): TimelineToolTaskItem {
  return {
    item_type: "TOOL_TASK",
    item_id: `item-${taskId}`,
    task_id: taskId,
    anchor_at: "2026-07-24T00:00:00Z",
    initial_user_message: {
      message_id: `message-${taskId}`,
      role: "USER",
      content_text: `input ${taskId}`,
      created_at: "2026-07-24T00:00:00Z",
    },
    task: {
      task_id: taskId,
      task_type: "TOOL_EXECUTION",
      status,
      selected_tool_run_id: null,
      selected_result_id: null,
      created_at: "2026-07-24T00:00:00Z",
      started_at: null,
      updated_at: updatedAt,
      completed_at: null,
      error_code: null,
      safe_error_message: null,
      tool_id: null,
      bound_tool_version: null,
      bound_schema_hash: null,
    },
    input_thread: [],
    input_thread_count: 0,
    input_thread_truncated: false,
    tool_runs: {
      attempt_count: 0,
      selected_tool_run: null,
      has_history: false,
    },
    result: null,
    assets: [],
    explanation: null,
    latest_explanation_failure: null,
    needs_input:
      status === "NEEDS_INPUT"
        ? {
            missing_fields: ["composition"],
            ambiguous_fields: [],
            normalized_input: null,
            candidate_tool_refs: [],
          }
        : null,
    errors: [],
    invocation: null,
  };
}

function timelinePage(
  conversationId: string,
  items: TimelinePage["items"] = [],
  nextCursor: string | null = null,
  hasMore = false,
): TimelinePage {
  return {
    conversation: {
      conversation_id: conversationId,
      title: conversationId,
    },
    items,
    next_cursor: nextCursor,
    has_more: hasMore,
  };
}

function taskDetail(taskId: string): TaskDetail {
  return {
    task_id: taskId,
    conversation_id: "conversation-1",
    task_type: "TOOL_EXECUTION",
    status: "SUCCEEDED",
    anchor_at: "2026-07-24T00:00:00Z",
    selected_tool_run_id: null,
    selected_result_id: null,
    created_at: "2026-07-24T00:00:00Z",
    started_at: null,
    updated_at: "2026-07-24T00:00:00Z",
    completed_at: null,
    error_code: null,
    safe_error_message: null,
    tool_id: "zta35g_sem_virtual_lab",
    bound_tool_version: "1",
    bound_schema_hash: "a".repeat(64),
    needs_input: null,
    tool_run_count: 0,
    tool_runs: [],
    selected_result_summary: null,
    assets: [],
    explanation_summary: null,
    latest_explanation_failure: null,
  };
}

function messageResponse(
  conversationId: string,
): MessageSubmissionResponseData {
  return {
    conversation_id: conversationId,
    user_message: {
      message_id: "message-1",
      role: "USER",
      content_text: "submitted",
      created_at: "2026-07-24T00:00:00Z",
    },
    task: {
      task_id: "task-1",
      task_type: "TOOL_EXECUTION",
      status: "PENDING",
      selected_tool_run_id: null,
      selected_result_id: null,
      created_at: "2026-07-24T00:00:00Z",
      updated_at: "2026-07-24T00:00:00Z",
    },
    assistant_message: null,
    needs_input: null,
    result_summary: null,
    explanation: null,
    latest_explanation_failure: null,
    tool_invocation: null,
    idempotency_replayed: false,
  };
}

function apiResponseError(
  status: number,
  message = `写操作失败 ${status}`,
): ApiResponseError {
  return new ApiResponseError(
    status,
    `request-action-${status}`,
    "ACTION_REJECTED",
    message,
    [],
    {
      conversation_id: "conversation-1",
      task_id: null,
      tool_run_id: null,
      result_id: null,
    },
  );
}

function fakeApi(): MaterialsAgentApi {
  return {
    createConversation: vi.fn<
      MaterialsAgentApi["createConversation"]
    >(() =>
      Promise.resolve(
        envelope<Conversation>({
          conversation_id: "conversation-created",
          title: null,
          created_at: "2026-07-24T00:00:00Z",
          updated_at: "2026-07-24T00:00:00Z",
        }),
      ),
    ),
    listConversations: vi.fn<
      MaterialsAgentApi["listConversations"]
    >(() => Promise.resolve(envelope(conversationPage([])))),
    getTimelinePage: vi.fn<
      MaterialsAgentApi["getTimelinePage"]
    >((conversationId) =>
      Promise.resolve(envelope(timelinePage(conversationId))),
    ),
    getTask: vi.fn<MaterialsAgentApi["getTask"]>((taskId) =>
      Promise.resolve(envelope(taskDetail(taskId))),
    ),
    getToolResult: vi.fn<MaterialsAgentApi["getToolResult"]>(() =>
      Promise.reject(new Error("not used")),
    ),
    getAssetMetadata: vi.fn<
      MaterialsAgentApi["getAssetMetadata"]
    >(() => Promise.reject(new Error("not used"))),
    submitMessage: vi.fn<MaterialsAgentApi["submitMessage"]>(
      (conversationId) =>
        Promise.resolve(envelope(messageResponse(conversationId))),
    ),
    retryTool: vi.fn<MaterialsAgentApi["retryTool"]>(() =>
      Promise.resolve(
        envelope<ToolRetryResponseData>({
          task_id: "task-1",
          task_status: "PENDING",
          tool_run: {
            tool_run_id: "tool-run-2",
            attempt_no: 2,
            status: "PENDING",
            task_input_revision_id: "revision-1",
          },
          result_id: null,
          result_status: null,
          explanation_id: null,
          explanation_status: null,
          idempotency_replayed: false,
        }),
      ),
    ),
    retryExplanation: vi.fn<
      MaterialsAgentApi["retryExplanation"]
    >(() =>
      Promise.resolve(
        envelope<ExplanationRetryResponseData>({
          task_id: "task-1",
          task_status: "SUCCEEDED",
          explanation: {
            explanation_id: "explanation-2",
            result_id: "result-1",
            attempt_no: 2,
            status: "SUCCEEDED",
            language: "zh-CN",
            text: "解释",
            error_code: null,
            safe_error_message: null,
            llm_call_id: "llm-call-2",
            llm_call_status: "SUCCEEDED",
          },
          idempotency_replayed: false,
        }),
      ),
    ),
    resolvePublicContentUrl: vi.fn((value: string) => value),
  };
}

function createSubject(
  api = fakeApi(),
  storage = new MemoryStorage(),
) {
  return {
    api,
    storage,
    agent: useMaterialsAgent({
      api,
      storage,
      keyFactory: () => "fixed-uuid",
    }),
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useMaterialsAgent", () => {
  it("initialize loads conversations without creating one", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.listConversations).mockResolvedValueOnce(
      envelope(conversationPage([conversation("conversation-1")])),
    );

    await agent.initialize();

    expect(agent.conversations.value.map((item) => item.conversation_id))
      .toEqual(["conversation-1"]);
    expect(api.createConversation).not.toHaveBeenCalled();
  });

  it("creates and confirms the first Conversation from the blank composer", async () => {
    const { agent, api } = createSubject();

    await agent.submitNewTask("  first material question  ");

    expect(api.createConversation).toHaveBeenCalledWith(
      undefined,
      "frontend-conversation-create-fixed-uuid",
    );
    expect(api.submitMessage).toHaveBeenCalledWith(
      "conversation-created",
      {
        submission_mode: "NEW_TASK",
        content_text: "first material question",
        target_task_id: null,
      },
      "frontend-task-create-fixed-uuid",
    );
    expect(agent.pendingMutation.value).toBeNull();
    expect(agent.selectedConversationId.value).toBe("conversation-created");
  });

  it("removes a deleted selected Conversation and clears its cached state", async () => {
    const { agent, api } = createSubject();
    api.deleteConversation = vi.fn(() =>
      Promise.resolve(
        envelope({ conversation_id: "conversation-1" }),
      ),
    );
    vi.mocked(api.listConversations).mockResolvedValueOnce(
      envelope(conversationPage([conversation("conversation-1")])),
    );
    await agent.loadConversations(true);
    await agent.selectConversation("conversation-1");
    agent.setSupplementTarget({
      conversationId: "conversation-1",
      taskId: "task-1",
      summary: "missing",
    });

    await agent.deleteConversation("conversation-1");

    expect(agent.conversations.value).toEqual([]);
    expect(agent.selectedConversationId.value).toBeNull();
    expect(agent.timeline.value).toEqual([]);
    expect(agent.supplementTarget.value).toBeNull();
  });

  it("keeps the server conversation order", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.listConversations).mockResolvedValueOnce(
      envelope(
        conversationPage([
          conversation("older-updated", "2026-01-01T00:00:00Z"),
          conversation("newer-updated", "2026-12-01T00:00:00Z"),
        ]),
      ),
    );

    await agent.loadConversations(true);

    expect(agent.conversations.value.map((item) => item.conversation_id))
      .toEqual(["older-updated", "newer-updated"]);
  });

  it("appends cursor pages and removes duplicate ids", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.listConversations)
      .mockResolvedValueOnce(
        envelope(
          conversationPage([conversation("conversation-1")], "cursor-2"),
        ),
      )
      .mockResolvedValueOnce(
        envelope(
          conversationPage([
            conversation("conversation-1"),
            conversation("conversation-2"),
          ]),
        ),
      );

    await agent.loadConversations(true);
    await agent.loadMoreConversations();

    expect(api.listConversations).toHaveBeenNthCalledWith(
      2,
      20,
      "cursor-2",
      expect.any(AbortSignal),
    );
    expect(agent.conversations.value.map((item) => item.conversation_id))
      .toEqual(["conversation-1", "conversation-2"]);
  });

  it("selects a newly created conversation and loads its timeline", async () => {
    const { agent, api } = createSubject();

    await agent.createConversation("Research");

    expect(api.createConversation).toHaveBeenCalledWith("Research");
    expect(agent.selectedConversationId.value).toBe(
      "conversation-created",
    );
    expect(api.getTimelinePage).toHaveBeenCalledWith(
      "conversation-created",
      50,
      undefined,
      expect.any(AbortSignal),
    );
  });

  it("keeps Conversation creation uncertainty visible through Timeline polling", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.createConversation).mockRejectedValueOnce(
      new ConversationCreationUncertaintyError(),
    );

    await expect(agent.createConversation()).rejects.toBeInstanceOf(
      ConversationCreationUncertaintyError,
    );
    expect(agent.conversationCreationUncertain.value).toBe(true);
    expect(agent.globalError.value?.message).toContain(
      "无法确认 Conversation 是否已创建",
    );

    vi.useFakeTimers();
    agent.startPolling();
    await Promise.resolve();
    await Promise.resolve();

    expect(api.getTimelinePage).toHaveBeenCalled();
    expect(agent.globalError.value?.message).toContain(
      "无法确认 Conversation 是否已创建",
    );
    agent.stopPolling();
  });

  it.each([409, 422, 500, 503, 504])(
    "refreshes authoritative facts after an explicit HTTP %s without replacing its action error",
    async (status) => {
      const { agent, api } = createSubject();
      await agent.selectConversation("conversation-1");
      vi.mocked(api.getTimelinePage).mockClear();
      vi.mocked(api.listConversations).mockClear();
      vi.mocked(api.submitMessage).mockClear();
      vi.mocked(api.getTimelinePage).mockResolvedValueOnce(
        envelope(
          timelinePage("conversation-1", [
            toolTask("authoritative-task", "FAILED"),
          ]),
          `request-timeline-${status}`,
        ),
      );
      const actionError = apiResponseError(status);
      vi.mocked(api.submitMessage).mockRejectedValueOnce(actionError);

      await expect(agent.submitNewTask("generate")).rejects.toBe(
        actionError,
      );
      expect(api.submitMessage).toHaveBeenCalledOnce();
      expect(api.getTimelinePage).toHaveBeenCalledOnce();
      expect(api.listConversations).toHaveBeenCalledOnce();
      expect(agent.timeline.value.map((item) => item.task_id)).toEqual([
        "authoritative-task",
      ]);
      expect(agent.globalErrors.value).toContainEqual({
        message: `写操作失败 ${status}`,
        status,
        request_id: `request-action-${status}`,
        details: [],
      });
      expect(agent.globalError.value).toMatchObject({
        message: `写操作失败 ${status}`,
        status,
        request_id: `request-action-${status}`,
      });
      expect(agent.mutationStatus.value).toBe("BUSINESS_FAILED");
      expect(agent.pendingMutation.value).toBeNull();
    },
  );

  it("keeps a 409 action error beside a failed authoritative Timeline read", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage).mockClear();
    vi.mocked(api.listConversations).mockClear();
    const actionError = apiResponseError(409, "任务状态已变化");
    vi.mocked(api.submitMessage).mockRejectedValueOnce(actionError);
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new ReadNetworkError(),
    );

    await expect(agent.submitNewTask("generate")).rejects.toBe(
      actionError,
    );

    expect(api.getTimelinePage).toHaveBeenCalledOnce();
    expect(agent.globalErrors.value).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          message: "任务状态已变化",
          status: 409,
        }),
        { message: "读取失败，请检查网络后重试。" },
      ]),
    );
  });

  it("refreshes Timeline and Conversation facts after a 422 supplement failure", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    agent.setSupplementTarget({
      conversationId: "conversation-1",
      taskId: "task-needs-input",
      summary: "missing input",
    });
    vi.mocked(api.getTimelinePage).mockClear();
    vi.mocked(api.listConversations).mockClear();
    const actionError = apiResponseError(422, "补充内容无效");
    vi.mocked(api.submitMessage).mockRejectedValueOnce(actionError);

    await expect(agent.submitSupplement("details")).rejects.toBe(
      actionError,
    );

    expect(api.getTimelinePage).toHaveBeenCalledOnce();
    expect(api.listConversations).toHaveBeenCalledOnce();
    expect(agent.supplementTarget.value).not.toBeNull();
  });

  it("refreshes authoritative facts after a pending retry gets a definite HTTP error", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage)
      .mockRejectedValueOnce(new NetworkUncertaintyError())
      .mockRejectedValueOnce(apiResponseError(409, "重放请求被拒绝"));
    await expect(
      agent.submitNewTask("generate"),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);
    vi.mocked(api.getTimelinePage).mockClear();
    vi.mocked(api.listConversations).mockClear();

    await expect(agent.retryPendingMutation()).rejects.toMatchObject({
      status: 409,
      message: "重放请求被拒绝",
    });

    expect(api.submitMessage).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.submitMessage).mock.calls[0]?.[2]).toBe(
      vi.mocked(api.submitMessage).mock.calls[1]?.[2],
    );
    expect(api.getTimelinePage).toHaveBeenCalledOnce();
    expect(api.listConversations).toHaveBeenCalledOnce();
    expect(agent.mutationStatus.value).toBe("BUSINESS_FAILED");
    expect(agent.pendingMutation.value).toBeNull();
    expect(agent.globalErrors.value).toContainEqual(
      expect.objectContaining({
        message: "重放请求被拒绝",
        status: 409,
      }),
    );
  });

  it("does not refresh authoritative facts for an UNCERTAIN network result", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage).mockClear();
    vi.mocked(api.listConversations).mockClear();
    vi.mocked(api.submitMessage).mockRejectedValueOnce(
      new NetworkUncertaintyError(),
    );

    await expect(
      agent.submitNewTask("generate"),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);

    expect(api.getTimelinePage).not.toHaveBeenCalled();
    expect(api.listConversations).not.toHaveBeenCalled();
    expect(agent.mutationStatus.value).toBe("UNCERTAIN");
  });

  it("clears a reconciliation warning after a successful Timeline refresh", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new ReadNetworkError(),
    );
    await agent.submitNewTask("generate");
    expect(agent.globalError.value?.message).toContain(
      "操作已成功，但界面刷新失败",
    );

    await agent.refreshTimeline();

    expect(agent.globalErrors.value).not.toContainEqual(
      expect.objectContaining({
        message:
          "操作已成功，但界面刷新失败。请手动刷新以查看最新状态。",
      }),
    );
    expect(agent.globalError.value).toBeNull();
  });

  it("retains a reconciliation warning when Timeline refresh still fails", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage)
      .mockRejectedValueOnce(new ReadNetworkError())
      .mockRejectedValueOnce(new ReadNetworkError());
    await agent.submitNewTask("generate");

    await expect(agent.refreshTimeline()).rejects.toBeInstanceOf(
      ReadNetworkError,
    );

    expect(agent.globalErrors.value).toEqual(
      expect.arrayContaining([
        {
          message:
            "操作已成功，但界面刷新失败。请手动刷新以查看最新状态。",
        },
        { message: "读取失败，请检查网络后重试。" },
      ]),
    );
  });

  it("keeps Conversation creation uncertainty visible beside a later 409", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.createConversation).mockRejectedValueOnce(
      new ConversationCreationUncertaintyError(),
    );
    await expect(agent.createConversation()).rejects.toBeInstanceOf(
      ConversationCreationUncertaintyError,
    );
    const actionError = apiResponseError(409, "Tool 当前不可重试");
    vi.mocked(api.retryTool).mockRejectedValueOnce(actionError);

    await expect(agent.retryTool("task-1")).rejects.toBe(actionError);

    expect(agent.conversationCreationUncertain.value).toBe(true);
    expect(agent.globalErrors.value).toEqual(
      expect.arrayContaining([
        {
          message:
            "无法确认 Conversation 是否已创建；请使用原幂等键重试，避免重复创建。",
        },
        expect.objectContaining({
          message: "Tool 当前不可重试",
          status: 409,
        }),
      ]),
    );
  });

  it("shows Conversation creation uncertainty beside a failed list refresh", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.createConversation).mockRejectedValueOnce(
      new ConversationCreationUncertaintyError(),
    );
    await expect(agent.createConversation()).rejects.toBeInstanceOf(
      ConversationCreationUncertaintyError,
    );
    vi.mocked(api.listConversations).mockRejectedValueOnce(
      new ReadNetworkError(),
    );

    await expect(agent.refreshConversations()).rejects.toBeInstanceOf(
      ReadNetworkError,
    );

    expect(agent.globalErrors.value).toEqual([
      {
        message:
          "无法确认 Conversation 是否已创建；请使用原幂等键重试，避免重复创建。",
      },
      { message: "读取失败，请检查网络后重试。" },
    ]);
  });

  it("successful list refresh removes only creation uncertainty and its read error", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.createConversation).mockRejectedValueOnce(
      new ConversationCreationUncertaintyError(),
    );
    await expect(agent.createConversation()).rejects.toBeInstanceOf(
      ConversationCreationUncertaintyError,
    );
    vi.mocked(api.listConversations).mockRejectedValueOnce(
      new ReadNetworkError(),
    );
    await expect(agent.refreshConversations()).rejects.toBeInstanceOf(
      ReadNetworkError,
    );
    const actionError = apiResponseError(409, "后续业务冲突");
    vi.mocked(api.retryTool).mockRejectedValueOnce(actionError);
    await expect(agent.retryTool("task-1")).rejects.toBe(actionError);
    expect(agent.globalErrors.value).toHaveLength(3);

    await agent.refreshConversations();

    expect(agent.conversationCreationUncertain.value).toBe(false);
    expect(agent.globalErrors.value).toEqual([
      {
        message: "后续业务冲突",
        status: 409,
        request_id: "request-action-409",
        details: [],
      },
    ]);
  });

  it("does not expose raw bodies or stacks through multiple notices", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage).mockRejectedValueOnce(
      apiResponseError(409, "安全业务错误"),
    );
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error(
        "raw body password=secret\n    at C:\\private\\internal.ts:1",
      ),
    );

    await expect(agent.submitNewTask("generate")).rejects.toMatchObject({
      status: 409,
    });

    const visible = JSON.stringify(agent.globalErrors.value);
    expect(visible).toContain("安全业务错误");
    expect(visible).toContain("请求处理失败。");
    expect(visible).not.toContain("raw body");
    expect(visible).not.toContain("secret");
    expect(visible).not.toContain("private");
    expect(visible).not.toContain("stack");
  });

  it("clears Conversation creation uncertainty only after an explicit list refresh succeeds", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.createConversation).mockRejectedValueOnce(
      new ConversationCreationUncertaintyError(),
    );
    await expect(agent.createConversation()).rejects.toBeInstanceOf(
      ConversationCreationUncertaintyError,
    );

    vi.mocked(api.listConversations).mockResolvedValueOnce(
      envelope(
        conversationPage([conversation("conversation-created")]),
        "request-refresh-conversations",
      ),
    );
    await agent.refreshConversations();

    expect(api.listConversations).toHaveBeenLastCalledWith(
      20,
      undefined,
      expect.any(AbortSignal),
    );
    expect(agent.conversationCreationUncertain.value).toBe(false);
    expect(agent.globalError.value).toBeNull();
    expect(agent.conversations.value.map((item) => item.conversation_id))
      .toEqual(["conversation-created"]);
  });

  it("retains Conversation creation uncertainty when the explicit list refresh fails", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.createConversation).mockRejectedValueOnce(
      new ConversationCreationUncertaintyError(),
    );
    await expect(agent.createConversation()).rejects.toBeInstanceOf(
      ConversationCreationUncertaintyError,
    );
    vi.mocked(api.listConversations).mockRejectedValueOnce(
      new ReadNetworkError(),
    );

    await expect(agent.refreshConversations()).rejects.toBeInstanceOf(
      ReadNetworkError,
    );

    expect(agent.conversationCreationUncertain.value).toBe(true);
    expect(agent.globalError.value?.message).toContain(
      "无法确认 Conversation 是否已创建",
    );
  });

  it("loads every timeline cursor page", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.getTimelinePage)
      .mockResolvedValueOnce(
        envelope(
          timelinePage(
            "conversation-1",
            [toolTask("task-1")],
            "cursor-2",
            true,
          ),
        ),
      )
      .mockResolvedValueOnce(
        envelope(
          timelinePage("conversation-1", [toolTask("task-2")]),
        ),
      );

    await agent.selectConversation("conversation-1");

    expect(api.getTimelinePage).toHaveBeenNthCalledWith(
      2,
      "conversation-1",
      50,
      "cursor-2",
      expect.any(AbortSignal),
    );
    expect(agent.timeline.value.map((item) => item.task_id)).toEqual([
      "task-1",
      "task-2",
    ]);
  });

  it("keeps timeline page and item order despite conflicting timestamps", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.getTimelinePage).mockResolvedValueOnce(
      envelope(
        timelinePage("conversation-1", [
          toolTask("server-first", "SUCCEEDED", "2026-12-01T00:00:00Z"),
          toolTask("server-second", "SUCCEEDED", "2026-01-01T00:00:00Z"),
        ]),
      ),
    );

    await agent.selectConversation("conversation-1");

    expect(agent.timeline.value.map((item) => item.task_id)).toEqual([
      "server-first",
      "server-second",
    ]);
  });

  it("does not duplicate a tool task initial user message", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.getTimelinePage).mockResolvedValueOnce(
      envelope(timelinePage("conversation-1", [toolTask("task-1")])),
    );

    await agent.selectConversation("conversation-1");

    expect(agent.timeline.value).toHaveLength(1);
    expect(agent.timeline.value[0]?.item_type).toBe("TOOL_TASK");
  });

  it("retains the previous complete timeline when a later page fails", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.getTimelinePage).mockResolvedValueOnce(
      envelope(timelinePage("conversation-1", [toolTask("existing")])),
    );
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage)
      .mockResolvedValueOnce(
        envelope(
          timelinePage(
            "conversation-1",
            [toolTask("partial")],
            "cursor-2",
            true,
          ),
        ),
      )
      .mockRejectedValueOnce(new Error("page two failed"));

    await expect(agent.refreshTimeline()).rejects.toThrow(
      "page two failed",
    );

    expect(agent.timeline.value.map((item) => item.task_id)).toEqual([
      "existing",
    ]);
  });

  it("rejects a repeated timeline cursor without looping", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.getTimelinePage)
      .mockResolvedValueOnce(
        envelope(timelinePage("conversation-1", [], "repeat", true)),
      )
      .mockResolvedValueOnce(
        envelope(timelinePage("conversation-1", [], "repeat", true)),
      );

    await expect(
      agent.selectConversation("conversation-1"),
    ).rejects.toThrow("分页游标");
    expect(api.getTimelinePage).toHaveBeenCalledTimes(2);
  });

  it("prevents a late conversation A response from replacing B", async () => {
    const { agent, api } = createSubject();
    const lateA = deferred<ApiSuccessEnvelope<TimelinePage>>();
    vi.mocked(api.getTimelinePage).mockImplementation(
      (conversationId) => {
        if (conversationId === "conversation-a") {
          return lateA.promise;
        }
        return Promise.resolve(
          envelope(
            timelinePage("conversation-b", [toolTask("task-b")]),
          ),
        );
      },
    );

    const selectingA = agent.selectConversation("conversation-a");
    await agent.selectConversation("conversation-b");
    lateA.resolve(
      envelope(timelinePage("conversation-a", [toolTask("task-a")])),
    );
    await selectingA;

    expect(agent.selectedConversationId.value).toBe("conversation-b");
    expect(agent.timeline.value.map((item) => item.task_id)).toEqual([
      "task-b",
    ]);
  });

  it("clears the supplement target when switching conversations", async () => {
    const { agent } = createSubject();
    agent.setSupplementTarget({
      conversationId: "conversation-a",
      taskId: "task-a",
      summary: "missing composition",
    });

    await agent.selectConversation("conversation-b");

    expect(agent.supplementTarget.value).toBeNull();
  });

  it("does not auto-select among multiple NEEDS_INPUT tasks", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.getTimelinePage).mockResolvedValueOnce(
      envelope(
        timelinePage("conversation-1", [
          toolTask("task-1", "NEEDS_INPUT"),
          toolTask("task-2", "NEEDS_INPUT"),
        ]),
      ),
    );

    await agent.selectConversation("conversation-1");

    expect(agent.supplementTarget.value).toBeNull();
  });

  it("sets and cancels an explicit supplement target", () => {
    const { agent } = createSubject();
    const target: SupplementTarget = {
      conversationId: "conversation-1",
      taskId: "task-1",
      summary: "missing composition",
    };

    agent.setSupplementTarget(target);
    expect(agent.supplementTarget.value).toEqual(target);
    agent.cancelSupplementTarget();
    expect(agent.supplementTarget.value).toBeNull();
  });

  it("submits a supplement with the explicit target task", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    agent.setSupplementTarget({
      conversationId: "conversation-1",
      taskId: "task-target",
      summary: "missing composition",
    });

    await agent.submitSupplement("  Zn 3.5%  ");

    expect(api.submitMessage).toHaveBeenCalledWith(
      "conversation-1",
      {
        submission_mode: "SUPPLEMENT_TASK",
        content_text: "Zn 3.5%",
        target_task_id: "task-target",
      },
      "frontend-task-input-supplement-fixed-uuid",
    );
  });

  it("does not send a supplement without a target", async () => {
    const { agent, api } = createSubject();

    await expect(agent.submitSupplement("Zn 3.5%")).rejects.toThrow(
      "补充目标",
    );

    expect(api.submitMessage).not.toHaveBeenCalled();
  });

  it("uses TASK_CREATE semantics for a trimmed new task", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");

    await agent.submitNewTask("  generate SEM  ");

    expect(api.submitMessage).toHaveBeenCalledWith(
      "conversation-1",
      {
        submission_mode: "NEW_TASK",
        content_text: "generate SEM",
        target_task_id: null,
      },
      "frontend-task-create-fixed-uuid",
    );
  });

  it("rejects blank new task text before sending", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");

    await expect(agent.submitNewTask("   ")).rejects.toThrow("不能为空");

    expect(api.submitMessage).not.toHaveBeenCalled();
  });

  it("uses TASK_INPUT_SUPPLEMENT key semantics", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    agent.setSupplementTarget({
      conversationId: "conversation-1",
      taskId: "task-1",
      summary: "missing",
    });

    await agent.submitSupplement("details");

    expect(vi.mocked(api.submitMessage).mock.calls[0]?.[2]).toContain(
      "task-input-supplement",
    );
  });

  it("uses the independent tool retry endpoint", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");

    await agent.retryTool("task-1");

    expect(api.retryTool).toHaveBeenCalledWith(
      "task-1",
      { reason: "USER_REQUESTED_RETRY" },
      "frontend-tool-retry-fixed-uuid",
    );
  });

  it("uses the independent explanation retry endpoint and language", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");

    await agent.retryExplanation("result-1", "en-US");

    expect(api.retryExplanation).toHaveBeenCalledWith(
      "result-1",
      {
        language: "en-US",
        reason: "USER_REQUESTED_RETRY",
      },
      "frontend-explanation-retry-fixed-uuid",
    );
    expect(api.retryTool).not.toHaveBeenCalled();
  });

  it("refreshes authoritative timeline immediately after a write", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage).mockClear();

    await agent.submitNewTask("generate");

    expect(api.getTimelinePage).toHaveBeenCalledOnce();
    expect(agent.lastRequestId.value).toBe("request-1");
  });

  it("retains a pending key after write network uncertainty", async () => {
    const { agent, api, storage } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage).mockRejectedValueOnce(
      new NetworkUncertaintyError(),
    );

    await expect(
      agent.submitNewTask("generate"),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);

    expect(agent.mutationStatus.value).toBe("UNCERTAIN");
    const pending = agent.pendingMutation.value;
    expect(pending?.operation).toBe("TASK_CREATE");
    if (pending?.operation !== "TASK_CREATE") {
      throw new Error("Expected a pending TASK_CREATE mutation.");
    }
    expect(pending.idempotencyKey).toBe(
      "frontend-task-create-fixed-uuid",
    );
    expect(storage.getItem(PENDING_MUTATION_STORAGE_KEY)).not.toBeNull();
  });

  it("retryPendingMutation reuses the original idempotency key", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage)
      .mockRejectedValueOnce(new NetworkUncertaintyError())
      .mockResolvedValueOnce(
        envelope(messageResponse("conversation-1"), "request-retry"),
      );
    await expect(
      agent.submitNewTask("generate"),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);

    await agent.retryPendingMutation();

    expect(vi.mocked(api.submitMessage).mock.calls[0]?.[2]).toBe(
      vi.mocked(api.submitMessage).mock.calls[1]?.[2],
    );
    expect(agent.pendingMutation.value).toBeNull();
  });

  it("turns an explicit 409 into a safe global error and clears pending", async () => {
    const { agent, api } = createSubject();
    const error = new ApiResponseError(
      409,
      "request-conflict",
      "TASK_STATE_CONFLICT",
      "任务状态已变化",
      [],
      {
        conversation_id: "conversation-1",
        task_id: null,
        tool_run_id: null,
        result_id: null,
      },
    );
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage).mockRejectedValueOnce(error);

    await expect(agent.submitNewTask("generate")).rejects.toBe(error);

    expect(agent.globalError.value).toEqual({
      message: "任务状态已变化",
      status: 409,
      request_id: "request-conflict",
      details: [],
    });
    expect(agent.mutationStatus.value).toBe("BUSINESS_FAILED");
    expect(agent.pendingMutation.value).toBeNull();
  });

  it("loads task history only when explicitly requested", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    expect(api.getTask).not.toHaveBeenCalled();

    await agent.loadTaskHistory("task-1");

    expect(api.getTask).toHaveBeenCalledOnce();
    expect(agent.taskDetailsById.value["task-1"]?.task_id).toBe("task-1");
  });

  it("invalidates task history after a tool retry", async () => {
    const { agent } = createSubject();
    await agent.selectConversation("conversation-1");
    await agent.loadTaskHistory("task-1");
    expect(agent.taskDetailsById.value["task-1"]).toBeDefined();

    await agent.retryTool("task-1");

    expect(agent.taskDetailsById.value["task-1"]).toBeUndefined();
  });

  it("multiple startPolling calls create one immediate poll", async () => {
    const { agent, api } = createSubject();
    vi.useFakeTimers();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage).mockClear();

    agent.startPolling();
    agent.startPolling();
    agent.startPolling();
    await Promise.resolve();
    await Promise.resolve();

    expect(api.getTimelinePage).toHaveBeenCalledOnce();
    agent.stopPolling();
  });

  it("pauses scheduled polling while the document is hidden", async () => {
    vi.useFakeTimers();
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage).mockClear();
    const visibility = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("visible");
    agent.startPolling();
    await Promise.resolve();
    await Promise.resolve();
    visibility.mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));

    await vi.advanceTimersByTimeAsync(60_000);

    expect(api.getTimelinePage).toHaveBeenCalledOnce();
    agent.stopPolling();
  });

  it("retains an existing timeline when refresh fails", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.getTimelinePage).mockResolvedValueOnce(
      envelope(timelinePage("conversation-1", [toolTask("existing")])),
    );
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error("refresh failed"),
    );

    await expect(agent.refreshTimeline()).rejects.toThrow(
      "refresh failed",
    );

    expect(agent.timeline.value.map((item) => item.task_id)).toEqual([
      "existing",
    ]);
  });

  it("never exposes raw bodies or stacks through globalError", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error("raw body password=secret\n    at internal/path.ts:1"),
    );

    await expect(agent.refreshTimeline()).rejects.toThrow();

    const visible = JSON.stringify(agent.globalError.value);
    expect(visible).toBe('{"message":"请求处理失败。"}');
    expect(visible).not.toContain("secret");
    expect(visible).not.toContain("internal/path");
  });

  it("keeps submitNewTask successful when authoritative refresh fails", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage).mockResolvedValueOnce(
      envelope(
        messageResponse("conversation-1"),
        "request-post-new-task",
      ),
    );
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error("private refresh failure"),
    );

    await expect(agent.submitNewTask("generate")).resolves.toBeUndefined();

    expect(agent.mutationStatus.value).toBe("SUCCEEDED");
    expect(agent.pendingMutation.value).toBeNull();
    expect(agent.lastRequestId.value).toBe("request-post-new-task");
    expect(agent.globalError.value).toEqual({
      message:
        "操作已成功，但界面刷新失败。请手动刷新以查看最新状态。",
    });
  });

  it("keeps a tool retry successful and invalidated when refresh fails", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    await agent.loadTaskHistory("task-1");
    vi.mocked(api.retryTool).mockResolvedValueOnce(
      envelope(
        {
          task_id: "task-1",
          task_status: "PENDING",
          tool_run: {
            tool_run_id: "tool-run-2",
            attempt_no: 2,
            status: "PENDING",
            task_input_revision_id: "revision-1",
          },
          result_id: null,
          result_status: null,
          explanation_id: null,
          explanation_status: null,
          idempotency_replayed: false,
        },
        "request-post-tool-retry",
      ),
    );
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error("private refresh failure"),
    );

    await expect(agent.retryTool("task-1")).resolves.toBeUndefined();

    expect(agent.mutationStatus.value).toBe("SUCCEEDED");
    expect(agent.pendingMutation.value).toBeNull();
    expect(agent.taskDetailsById.value["task-1"]).toBeUndefined();
    expect(agent.lastRequestId.value).toBe("request-post-tool-retry");
    expect(agent.globalError.value?.message).toContain(
      "操作已成功，但界面刷新失败",
    );
  });

  it("keeps an explanation retry successful when refresh fails", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.retryExplanation).mockResolvedValueOnce(
      envelope(
        {
          task_id: "task-1",
          task_status: "SUCCEEDED",
          explanation: {
            explanation_id: "explanation-2",
            result_id: "result-1",
            attempt_no: 2,
            status: "SUCCEEDED",
            language: "zh-CN",
            text: "解释",
            error_code: null,
            safe_error_message: null,
            llm_call_id: "llm-call-2",
            llm_call_status: "SUCCEEDED",
          },
          idempotency_replayed: false,
        },
        "request-post-explanation-retry",
      ),
    );
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error("private refresh failure"),
    );

    await expect(
      agent.retryExplanation("result-1"),
    ).resolves.toBeUndefined();

    expect(agent.mutationStatus.value).toBe("SUCCEEDED");
    expect(agent.pendingMutation.value).toBeNull();
    expect(agent.lastRequestId.value).toBe(
      "request-post-explanation-retry",
    );
    expect(agent.globalError.value?.message).toContain(
      "操作已成功，但界面刷新失败",
    );
  });

  it("keeps retryPendingMutation successful when refresh fails", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage)
      .mockRejectedValueOnce(new NetworkUncertaintyError())
      .mockResolvedValueOnce(
        envelope(
          messageResponse("conversation-1"),
          "request-post-pending-retry",
        ),
      );
    await expect(
      agent.submitNewTask("generate"),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error("private refresh failure"),
    );

    await expect(
      agent.retryPendingMutation(),
    ).resolves.toBeUndefined();

    expect(agent.mutationStatus.value).toBe("SUCCEEDED");
    expect(agent.pendingMutation.value).toBeNull();
    expect(agent.lastRequestId.value).toBe(
      "request-post-pending-retry",
    );
    expect(agent.globalError.value?.message).toContain(
      "操作已成功，但界面刷新失败",
    );
  });

  it("clears supplement target after confirmed POST even when refresh fails", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    agent.setSupplementTarget({
      conversationId: "conversation-1",
      taskId: "task-1",
      summary: "missing input",
    });
    vi.mocked(api.submitMessage).mockResolvedValueOnce(
      envelope(
        messageResponse("conversation-1"),
        "request-post-supplement",
      ),
    );
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error("private refresh failure"),
    );

    await expect(
      agent.submitSupplement("details"),
    ).resolves.toBeUndefined();

    expect(agent.mutationStatus.value).toBe("SUCCEEDED");
    expect(agent.supplementTarget.value).toBeNull();
    expect(agent.lastRequestId.value).toBe("request-post-supplement");
    expect(agent.globalError.value?.message).toContain(
      "操作已成功，但界面刷新失败",
    );
  });

  it.each(["list", "timeline"] as const)(
    "returns a confirmed Conversation when %s reconciliation fails",
    async (failurePoint) => {
      const { agent, api } = createSubject();
      vi.mocked(api.createConversation).mockResolvedValueOnce(
        envelope(
          {
            conversation_id: "conversation-created",
            title: "Created",
            created_at: "2026-07-24T00:00:00Z",
            updated_at: "2026-07-24T00:00:00Z",
          },
          "request-post-conversation",
        ),
      );
      if (failurePoint === "list") {
        vi.mocked(api.listConversations).mockRejectedValueOnce(
          new Error("private list failure"),
        );
      } else {
        vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
          new Error("private timeline failure"),
        );
      }

      await expect(
        agent.createConversation("Created"),
      ).resolves.toMatchObject({
        conversation_id: "conversation-created",
      });

      expect(agent.selectedConversationId.value).toBe(
        "conversation-created",
      );
      expect(agent.lastRequestId.value).toBe(
        "request-post-conversation",
      );
      expect(agent.globalError.value?.message).toContain(
        "操作已成功，但界面刷新失败",
      );
    },
  );

  it("clears Conversation A Timeline before failed switch to B", async () => {
    const { agent, api } = createSubject();
    vi.mocked(api.getTimelinePage).mockResolvedValueOnce(
      envelope(timelinePage("conversation-a", [toolTask("task-a")])),
    );
    await agent.selectConversation("conversation-a");
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error("conversation B failed"),
    );

    await expect(
      agent.selectConversation("conversation-b"),
    ).rejects.toThrow("conversation B failed");

    expect(agent.selectedConversationId.value).toBe("conversation-b");
    expect(agent.timeline.value).toEqual([]);
  });

  it("does not let stale Task history repopulate cache after retry", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    const stale = deferred<ApiSuccessEnvelope<TaskDetail>>();
    vi.mocked(api.getTask).mockReturnValueOnce(stale.promise);

    const loading = agent.loadTaskHistory("task-1");
    const taskSignal = vi.mocked(api.getTask).mock.calls[0]?.[1];
    await agent.retryTool("task-1");
    stale.resolve(envelope(taskDetail("task-1"), "request-stale-task"));
    await loading;

    expect(taskSignal?.aborted).toBe(true);
    expect(agent.taskDetailsById.value["task-1"]).toBeUndefined();
  });

  it("keeps Task history loading true until the latest request completes", async () => {
    const { agent, api } = createSubject();
    const first = deferred<ApiSuccessEnvelope<TaskDetail>>();
    const second = deferred<ApiSuccessEnvelope<TaskDetail>>();
    vi.mocked(api.getTask)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const firstLoading = agent.loadTaskHistory("task-1");
    const secondLoading = agent.loadTaskHistory("task-1");
    first.resolve(envelope(taskDetail("task-1"), "request-task-first"));
    await firstLoading;

    expect(agent.taskDetailsLoadingById.value["task-1"]).toBe(true);

    second.resolve(
      envelope(taskDetail("task-1"), "request-task-second"),
    );
    await secondLoading;
    expect(agent.taskDetailsLoadingById.value["task-1"]).toBe(false);
  });

  it("does not clear an UNCERTAIN write warning after a successful GET", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage).mockRejectedValueOnce(
      new NetworkUncertaintyError(),
    );
    await expect(
      agent.submitNewTask("generate"),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);

    await agent.refreshTimeline();

    expect(agent.mutationStatus.value).toBe("UNCERTAIN");
    expect(agent.globalError.value?.message).toContain("原幂等键");
  });

  it("clears the UNCERTAIN warning when its pending mutation is discarded", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage).mockRejectedValueOnce(
      new NetworkUncertaintyError(),
    );
    await expect(
      agent.submitNewTask("generate"),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);

    expect(agent.pendingMutation.value).not.toBeNull();
    expect(agent.globalError.value?.message).toContain("原幂等键");
    expect(agent.discardPendingMutation()).toEqual({ discarded: true });

    expect(agent.mutationStatus.value).toBe("IDLE");
    expect(agent.pendingMutation.value).toBeNull();
    expect(agent.globalError.value).toBeNull();
  });

  it("keeps an unrelated error when there is no pending mutation to discard", async () => {
    const { agent, api } = createSubject();
    await agent.selectConversation("conversation-1");
    vi.mocked(api.getTimelinePage).mockRejectedValueOnce(
      new Error("private read failure"),
    );
    await expect(agent.refreshTimeline()).rejects.toThrow();
    const previousError = agent.globalError.value;

    expect(agent.discardPendingMutation()).toEqual({
      discarded: false,
    });
    expect(agent.globalError.value).toEqual(previousError);
  });

  it("does not restart polling after scope disposal during a Conversation switch", async () => {
    vi.useFakeTimers();
    const api = fakeApi();
    const scope = effectScope();
    const agent = scope.run(() =>
      useMaterialsAgent({
        api,
        storage: new MemoryStorage(),
        keyFactory: () => "fixed-uuid",
      }),
    );
    expect(agent).toBeDefined();
    if (agent === undefined) {
      return;
    }
    await agent.selectConversation("conversation-a");
    vi.mocked(api.getTimelinePage).mockClear();
    const addListener = vi.spyOn(document, "addEventListener");
    const removeListener = vi.spyOn(document, "removeEventListener");

    agent.startPolling();
    await Promise.resolve();
    await Promise.resolve();
    expect(api.getTimelinePage).toHaveBeenCalledOnce();

    const lateConversationB =
      deferred<ApiSuccessEnvelope<TimelinePage>>();
    vi.mocked(api.getTimelinePage).mockReturnValueOnce(
      lateConversationB.promise,
    );
    const switching = agent.selectConversation("conversation-b");
    const conversationBSignal =
      vi.mocked(api.getTimelinePage).mock.calls.at(-1)?.[3];
    const callsAtDispose =
      vi.mocked(api.getTimelinePage).mock.calls.length;

    scope.stop();
    expect(conversationBSignal?.aborted).toBe(true);
    lateConversationB.resolve(
      envelope(
        timelinePage("conversation-b", [toolTask("late-b")]),
        "request-late-b",
      ),
    );
    await switching;
    await vi.advanceTimersByTimeAsync(60_000);
    document.dispatchEvent(new Event("visibilitychange"));
    await Promise.resolve();
    await Promise.resolve();

    const visibilityAdds = addListener.mock.calls.filter(
      ([type]) => type === "visibilitychange",
    );
    const visibilityRemoves = removeListener.mock.calls.filter(
      ([type]) => type === "visibilitychange",
    );
    expect(agent.timeline.value).toEqual([]);
    expect(api.getTimelinePage).toHaveBeenCalledTimes(callsAtDispose);
    expect(visibilityRemoves).toHaveLength(visibilityAdds.length);
  });

  it("does not reconcile an idempotent POST that succeeds after scope disposal", async () => {
    const api = fakeApi();
    const scope = effectScope();
    const agent = scope.run(() =>
      useMaterialsAgent({
        api,
        storage: new MemoryStorage(),
        keyFactory: () => "fixed-uuid",
      }),
    );
    expect(agent).toBeDefined();
    if (agent === undefined) {
      return;
    }
    await agent.selectConversation("conversation-1");
    const requestIdBeforePost = agent.lastRequestId.value;
    const latePost =
      deferred<ApiSuccessEnvelope<MessageSubmissionResponseData>>();
    vi.mocked(api.submitMessage).mockReturnValueOnce(latePost.promise);
    vi.mocked(api.getTimelinePage).mockClear();
    vi.mocked(api.listConversations).mockClear();

    const submitting = agent.submitNewTask("generate");
    scope.stop();
    latePost.resolve(
      envelope(
        messageResponse("conversation-1"),
        "request-late-post",
      ),
    );
    await expect(submitting).resolves.toBeUndefined();

    expect(api.submitMessage).toHaveBeenCalledOnce();
    expect(api.getTimelinePage).not.toHaveBeenCalled();
    expect(api.listConversations).not.toHaveBeenCalled();
    expect(agent.mutationStatus.value).toBe("SUCCEEDED");
    expect(agent.pendingMutation.value).toBeNull();
    expect(agent.lastRequestId.value).toBe(requestIdBeforePost);
  });

  it("does not reconcile a pending retry that succeeds after scope disposal", async () => {
    const api = fakeApi();
    const scope = effectScope();
    const agent = scope.run(() =>
      useMaterialsAgent({
        api,
        storage: new MemoryStorage(),
        keyFactory: () => "fixed-uuid",
      }),
    );
    expect(agent).toBeDefined();
    if (agent === undefined) {
      return;
    }
    await agent.selectConversation("conversation-1");
    vi.mocked(api.submitMessage).mockRejectedValueOnce(
      new NetworkUncertaintyError(),
    );
    await expect(
      agent.submitNewTask("generate"),
    ).rejects.toBeInstanceOf(NetworkUncertaintyError);
    const requestIdBeforeRetry = agent.lastRequestId.value;
    const lateRetry =
      deferred<ApiSuccessEnvelope<MessageSubmissionResponseData>>();
    vi.mocked(api.submitMessage).mockReturnValueOnce(lateRetry.promise);
    vi.mocked(api.getTimelinePage).mockClear();
    vi.mocked(api.listConversations).mockClear();

    const retrying = agent.retryPendingMutation();
    scope.stop();
    lateRetry.resolve(
      envelope(
        messageResponse("conversation-1"),
        "request-late-retry",
      ),
    );
    await expect(retrying).resolves.toBeUndefined();

    expect(api.submitMessage).toHaveBeenCalledTimes(2);
    expect(api.getTimelinePage).not.toHaveBeenCalled();
    expect(api.listConversations).not.toHaveBeenCalled();
    expect(agent.mutationStatus.value).toBe("SUCCEEDED");
    expect(agent.pendingMutation.value).toBeNull();
    expect(agent.lastRequestId.value).toBe(requestIdBeforeRetry);
  });

  it("returns a created Conversation without GETs when its POST succeeds after disposal", async () => {
    const api = fakeApi();
    const scope = effectScope();
    const agent = scope.run(() =>
      useMaterialsAgent({
        api,
        storage: new MemoryStorage(),
        keyFactory: () => "fixed-uuid",
      }),
    );
    expect(agent).toBeDefined();
    if (agent === undefined) {
      return;
    }
    const lateCreate = deferred<ApiSuccessEnvelope<Conversation>>();
    vi.mocked(api.createConversation).mockReturnValueOnce(
      lateCreate.promise,
    );

    const creating = agent.createConversation("Created");
    scope.stop();
    lateCreate.resolve(
      envelope(
        {
          conversation_id: "conversation-created-late",
          title: "Created",
          created_at: "2026-07-24T00:00:00Z",
          updated_at: "2026-07-24T00:00:00Z",
        },
        "request-late-create",
      ),
    );

    await expect(creating).resolves.toMatchObject({
      conversation_id: "conversation-created-late",
    });
    expect(api.createConversation).toHaveBeenCalledOnce();
    expect(api.listConversations).not.toHaveBeenCalled();
    expect(api.getTimelinePage).not.toHaveBeenCalled();
    expect(agent.lastRequestId.value).toBeNull();
  });

  it("disposes polling and ignores its late Timeline response with Vue scope", async () => {
    vi.useFakeTimers();
    const api = fakeApi();
    const scope = effectScope();
    const agent = scope.run(() =>
      useMaterialsAgent({
        api,
        storage: new MemoryStorage(),
        keyFactory: () => "fixed-uuid",
      }),
    );
    expect(agent).toBeDefined();
    if (agent === undefined) {
      return;
    }
    await agent.selectConversation("conversation-1");
    const existing = [...agent.timeline.value];
    const late = deferred<ApiSuccessEnvelope<TimelinePage>>();
    vi.mocked(api.getTimelinePage).mockReturnValueOnce(late.promise);
    agent.startPolling();
    const pollSignal =
      vi.mocked(api.getTimelinePage).mock.calls.at(-1)?.[3];

    scope.stop();
    expect(pollSignal?.aborted).toBe(true);
    late.resolve(
      envelope(
        timelinePage("conversation-1", [toolTask("late-task")]),
      ),
    );
    await Promise.resolve();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(agent.timeline.value).toEqual(existing);
    expect(api.getTimelinePage).toHaveBeenCalledTimes(2);
  });

  it("aborts Conversation, Timeline, and all Task history GETs on scope dispose", () => {
    const api = fakeApi();
    vi.mocked(api.listConversations).mockImplementation(
      () => new Promise(() => undefined),
    );
    vi.mocked(api.getTimelinePage).mockImplementation(
      () => new Promise(() => undefined),
    );
    vi.mocked(api.getTask).mockImplementation(
      () => new Promise(() => undefined),
    );
    const scope = effectScope();
    const agent = scope.run(() =>
      useMaterialsAgent({
        api,
        storage: new MemoryStorage(),
        keyFactory: () => "fixed-uuid",
      }),
    );
    expect(agent).toBeDefined();
    if (agent === undefined) {
      return;
    }

    void agent.loadConversations(true);
    void agent.selectConversation("conversation-1");
    void agent.loadTaskHistory("task-1");
    void agent.loadTaskHistory("task-2");
    const conversationSignal =
      vi.mocked(api.listConversations).mock.calls[0]?.[2];
    const timelineSignal =
      vi.mocked(api.getTimelinePage).mock.calls[0]?.[3];
    const taskSignals = vi.mocked(api.getTask).mock.calls.map(
      (call) => call[1],
    );

    scope.stop();

    expect(conversationSignal?.aborted).toBe(true);
    expect(timelineSignal?.aborted).toBe(true);
    expect(taskSignals).toHaveLength(2);
    expect(taskSignals.every((signal) => signal?.aborted)).toBe(true);
  });
});
