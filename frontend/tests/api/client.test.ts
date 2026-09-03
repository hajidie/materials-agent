import { describe, expect, expectTypeOf, it, vi } from "vitest";

import {
  createMaterialsAgentApi,
  withAttachmentDisposition,
} from "../../src/api/client";
import {
  ApiResponseError,
  ConversationCreationUncertaintyError,
  NetworkUncertaintyError,
  ProtocolResponseError,
  RequestAbortedError,
  toUserVisibleError,
} from "../../src/api/errors";
import type {
  ResultSummary,
  ToolResult,
} from "../../src/api/types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function success<T>(requestId: string, data: T): Response {
  return jsonResponse({ request_id: requestId, data });
}

function errorResponse(status: number): Response {
  return jsonResponse(
    {
      request_id: `request-${status}`,
      error: {
        code: `ERROR_${status}`,
        message: `安全错误 ${status}`,
        details: [
          {
            field: "content_text",
            code: "INVALID",
            message: "字段值无效。",
          },
        ],
      },
      resource: {
        conversation_id: "conversation-1",
        task_id: "task-1",
        tool_run_id: null,
        result_id: null,
      },
    },
    status,
  );
}

describe("createMaterialsAgentApi", () => {
  it("models Backend result warnings as unknown arrays", () => {
    expectTypeOf<ResultSummary["warnings"]>()
      .toEqualTypeOf<unknown[]>();
    expectTypeOf<ToolResult["warnings"]>()
      .toEqualTypeOf<unknown[]>();
  });

  it("parses a successful JSON envelope and request_id", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      success("request-create", {
        conversation_id: "conversation-1",
        title: null,
        created_at: "2026-07-24T10:00:00Z",
        updated_at: "2026-07-24T10:00:00Z",
      }),
    );
    const api = createMaterialsAgentApi({ fetchImpl, baseUrl: "/api/v1/" });

    const result = await api.createConversation();

    expect(result.request_id).toBe("request-create");
    expect(result.data.conversation_id).toBe("conversation-1");
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/conversations",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({}),
      }),
    );
  });

  it("reuses the caller-supplied Conversation idempotency key", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      success("request-create", {
        conversation_id: "conversation-1",
        title: null,
        created_at: "2026-07-24T10:00:00Z",
        updated_at: "2026-07-24T10:00:00Z",
      }),
    );
    const api = createMaterialsAgentApi({ fetchImpl });

    await api.createConversation(undefined, "stable-conversation-key");

    const init = fetchImpl.mock.calls[0]?.[1];
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBe(
      "stable-conversation-key",
    );
  });

  it("deletes the exact encoded Conversation resource without a request body", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      success("request-delete", { conversation_id: "conversation/one" }),
    );
    const api = createMaterialsAgentApi({ fetchImpl });

    await api.deleteConversation?.("conversation/one");

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/conversations/conversation%2Fone",
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(fetchImpl.mock.calls[0]?.[1]?.body).toBeUndefined();
  });

  it("preserves the Conversation list cursor and server item order", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      success("request-list", {
        items: [
          {
            conversation_id: "conversation-z",
            title: "后端第一项",
            created_at: "2026-07-24T10:00:00Z",
            updated_at: "2026-07-24T12:00:00Z",
            last_activity_preview: "第一项",
          },
          {
            conversation_id: "conversation-a",
            title: "后端第二项",
            created_at: "2026-07-24T11:00:00Z",
            updated_at: "2026-07-24T13:00:00Z",
            last_activity_preview: null,
          },
        ],
        next_cursor: "cursor-next",
      }),
    );
    const api = createMaterialsAgentApi({ fetchImpl });

    const result = await api.listConversations(50, "cursor current");

    expect(result.data.items.map((item) => item.conversation_id)).toEqual([
      "conversation-z",
      "conversation-a",
    ]);
    expect(result.data.next_cursor).toBe("cursor-next");
    expect(fetchImpl.mock.calls[0]?.[0]).toBe(
      "/api/v1/conversations?limit=50&cursor=cursor+current",
    );
  });

  it("returns Timeline items with their discriminated item_type", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      success("request-timeline", {
        conversation: {
          conversation_id: "conversation-1",
          title: "时间线",
        },
        items: [
          {
            item_type: "USER_MESSAGE",
            item_id: "message-1",
            task_id: "task-1",
            anchor_at: "2026-07-24T10:00:00Z",
            message: {
              message_id: "message-1",
              role: "USER",
              content_text: "知识问题",
              created_at: "2026-07-24T10:00:00Z",
            },
          },
        ],
        next_cursor: null,
        has_more: false,
      }),
    );
    const api = createMaterialsAgentApi({ fetchImpl });

    const result = await api.getTimelinePage("conversation-1", 50);

    expect(result.data.items[0]?.item_type).toBe("USER_MESSAGE");
    expect(fetchImpl.mock.calls[0]?.[0]).toBe(
      "/api/v1/conversations/conversation-1/timeline?limit=50",
    );
  });

  it.each([400, 404, 409, 422, 500, 503, 504])(
    "keeps safe public error fields for HTTP %i",
    async (status) => {
      const fetchImpl = vi
        .fn<typeof fetch>()
        .mockResolvedValue(errorResponse(status));
      const api = createMaterialsAgentApi({ fetchImpl });

      const promise = api.getTask("task-1");

      await expect(promise).rejects.toMatchObject({
        status,
        requestId: `request-${status}`,
        code: `ERROR_${status}`,
        message: `安全错误 ${status}`,
        details: [
          {
            field: "content_text",
            code: "INVALID",
            message: "字段值无效。",
          },
        ],
        resource: {
          conversation_id: "conversation-1",
          task_id: "task-1",
          tool_run_id: null,
          result_id: null,
        },
      });
      await expect(promise).rejects.toBeInstanceOf(ApiResponseError);
    },
  );

  it("classifies fetch failures as network uncertainty", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("private network detail"));
    const api = createMaterialsAgentApi({ fetchImpl });

    await expect(api.submitMessage(
      "conversation-1",
      {
        submission_mode: "NEW_TASK",
        content_text: "请求",
        target_task_id: null,
      },
      "opaque-key",
    )).rejects.toBeInstanceOf(NetworkUncertaintyError);
  });

  it("classifies Timeline GET network failure as a safe read error", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("private read network detail"));
    const api = createMaterialsAgentApi({ fetchImpl });

    const promise = api.getTimelinePage("conversation-1", 50);

    await expect(promise).rejects.toMatchObject({
      kind: "READ_NETWORK_ERROR",
      message: "读取失败，请检查网络后重试。",
    });
    await expect(promise).rejects.not.toBeInstanceOf(
      NetworkUncertaintyError,
    );
  });

  it("classifies Conversation creation network failure without suggesting a duplicate create", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("private create network detail"));
    const api = createMaterialsAgentApi({ fetchImpl });

    await expect(api.createConversation()).rejects.toMatchObject({
      kind: "CONVERSATION_CREATION_UNCERTAINTY",
      message:
        "无法确认 Conversation 是否已创建；请使用原幂等键重试，避免重复创建。",
    });
  });

  it("keeps all four idempotent write operations as network uncertainty", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new TypeError("private write network detail"));
    const api = createMaterialsAgentApi({ fetchImpl });
    const requests: Array<() => Promise<unknown>> = [
      () => api.submitMessage(
        "conversation-1",
        {
          submission_mode: "NEW_TASK",
          content_text: "新任务",
          target_task_id: null,
        },
        "new-task-key",
      ),
      () => api.submitMessage(
        "conversation-1",
        {
          submission_mode: "SUPPLEMENT_TASK",
          content_text: "补充",
          target_task_id: "task-1",
        },
        "supplement-key",
      ),
      () => api.retryTool(
        "task-1",
        { reason: "USER_REQUESTED_RETRY" },
        "tool-retry-key",
      ),
      () => api.retryExplanation(
        "result-1",
        {
          language: "zh-CN",
          reason: "USER_REQUESTED_RETRY",
        },
        "explanation-retry-key",
      ),
    ];

    for (const request of requests) {
      await expect(request()).rejects.toBeInstanceOf(
        NetworkUncertaintyError,
      );
    }
  });

  it("classifies a non-JSON success response as a protocol error", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("<html>private upstream body</html>", {
        status: 200,
        headers: { "Content-Type": "text/html" },
      }),
    );
    const api = createMaterialsAgentApi({ fetchImpl });

    await expect(api.getTask("task-1")).rejects.toBeInstanceOf(
      ProtocolResponseError,
    );
  });

  it("classifies a non-JSON error response as a protocol error", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("<html>private proxy error</html>", {
        status: 503,
        headers: { "Content-Type": "text/html" },
      }),
    );
    const api = createMaterialsAgentApi({ fetchImpl });

    await expect(api.getTask("task-1")).rejects.toBeInstanceOf(
      ProtocolResponseError,
    );
  });

  it.each([200, 500, 503])(
    "classifies a non-JSON Conversation creation HTTP %i as uncertain",
    async (status) => {
      const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
        new Response("<html>private create response</html>", {
          status,
          headers: { "Content-Type": "text/html" },
        }),
      );
      const api = createMaterialsAgentApi({ fetchImpl });
      const promise = api.createConversation();

      await expect(promise).rejects.toBeInstanceOf(
        ConversationCreationUncertaintyError,
      );
      await expect(promise).rejects.toMatchObject({
        kind: "CONVERSATION_CREATION_UNCERTAINTY",
        message:
          "无法确认 Conversation 是否已创建；请使用原幂等键重试，避免重复创建。",
      });
      await expect(promise).rejects.not.toMatchObject({
        message: expect.stringContaining("private create response"),
      });
    },
  );

  it("keeps a non-JSON idempotent write response as a protocol error", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("<html>private write response</html>", {
        status: 503,
        headers: { "Content-Type": "text/html" },
      }),
    );
    const api = createMaterialsAgentApi({ fetchImpl });

    await expect(
      api.submitMessage(
        "conversation-1",
        {
          submission_mode: "NEW_TASK",
          content_text: "请求",
          target_task_id: null,
        },
        "opaque-key",
      ),
    ).rejects.toBeInstanceOf(ProtocolResponseError);
  });

  it("adds JSON and Idempotency-Key headers to writes", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      success("request-write", {
        task_id: "task-1",
        task_status: "FAILED",
        tool_run: {
          tool_run_id: "run-2",
          attempt_no: 2,
          status: "FAILED",
          task_input_revision_id: "revision-1",
        },
        result_id: null,
        result_status: null,
        explanation_id: null,
        explanation_status: null,
        idempotency_replayed: false,
      }),
    );
    const api = createMaterialsAgentApi({ fetchImpl });

    await api.retryTool(
      "task-1",
      { reason: "USER_REQUESTED_RETRY" },
      "retry-key",
    );

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/v1/tasks/task-1/tool-runs",
      expect.objectContaining({
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": "retry-key",
        },
      }),
    );
  });

  it("does not add Idempotency-Key to GET requests", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      success("request-asset", {
        asset_id: "asset-1",
        status: "AVAILABLE",
        asset_type: "sem_image",
        source_type: "GENERATED",
        producer_tool_run_id: "run-1",
        role: "requested_output",
        media_type: "image/png",
        width: 512,
        height: 512,
        bit_depth: 8,
        size_bytes: 1024,
        sha256: "a".repeat(64),
        created_at: "2026-07-24T10:00:00Z",
        available_at: "2026-07-24T10:00:01Z",
        error_code: null,
        safe_error_message: null,
      }),
    );
    const api = createMaterialsAgentApi({ fetchImpl });

    await api.getAssetMetadata("asset-1");

    const init = fetchImpl.mock.calls[0]?.[1];
    expect(new Headers(init?.headers).has("Idempotency-Key")).toBe(false);
  });

  it("normalizes an absolute API base trailing slash", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      success("request-task", {
        task_id: "task-1",
        conversation_id: "conversation-1",
        task_type: null,
        status: "PENDING",
        anchor_at: "2026-07-24T10:00:00Z",
        selected_tool_run_id: null,
        selected_result_id: null,
        created_at: "2026-07-24T10:00:00Z",
        started_at: null,
        updated_at: "2026-07-24T10:00:00Z",
        completed_at: null,
        error_code: null,
        safe_error_message: null,
        needs_input: null,
        tool_run_count: 0,
        tool_runs: [],
        selected_result_summary: null,
        assets: [],
        explanation_summary: null,
        latest_explanation_failure: null,
      }),
    );
    const api = createMaterialsAgentApi({
      fetchImpl,
      baseUrl: "https://example.test/api/v1/",
    });

    await api.getTask("task-1");

    expect(fetchImpl.mock.calls[0]?.[0]).toBe(
      "https://example.test/api/v1/tasks/task-1",
    );
  });

  it.each(["", "   ", "javascript:alert(1)", "file:///private"])(
    "rejects an unsafe or empty API base %j",
    (baseUrl) => {
      expect(() => createMaterialsAgentApi({ baseUrl })).toThrow(
        "API base URL",
      );
    },
  );

  it("resolves public Asset content URLs without constructing storage URLs", () => {
    const relativeApi = createMaterialsAgentApi({ baseUrl: "/api/v1" });
    const absoluteApi = createMaterialsAgentApi({
      baseUrl: "https://api.example.test/api/v1",
    });

    expect(
      relativeApi.resolvePublicContentUrl(
        "/api/v1/assets/asset-1/content",
      ),
    ).toBe("/api/v1/assets/asset-1/content");
    expect(
      absoluteApi.resolvePublicContentUrl(
        "/api/v1/assets/asset-1/content",
      ),
    ).toBe("https://api.example.test/api/v1/assets/asset-1/content");
    expect(() =>
      relativeApi.resolvePublicContentUrl("javascript:alert(1)"),
    ).toThrow("content URL");
    expect(() =>
      relativeApi.resolvePublicContentUrl("data:image/png;base64,AA=="),
    ).toThrow("content URL");
  });

  it("resolves only root-relative public Asset content URLs to an absolute API origin", () => {
    const api = createMaterialsAgentApi({
      baseUrl: "https://api.example.test/api/v1",
    });

    expect(
      api.resolvePublicContentUrl(
        "/api/v1/assets/asset-1/content?preview=1",
      ),
    ).toBe(
      "https://api.example.test/api/v1/assets/asset-1/content?preview=1",
    );
  });

  it.each([
    "https://evil.example/image.png",
    "//evil.example/image.png",
    "javascript:alert(1)",
    "data:image/png;base64,AA==",
    "/api/v1/tasks/task-1",
    "/api/v1/assets/asset-1",
    "/api/v1/assets/asset-1/metadata",
  ])("rejects non-public Asset content URL %j", (contentUrl) => {
    const api = createMaterialsAgentApi({
      baseUrl: "https://api.example.test/api/v1",
    });

    expect(() => api.resolvePublicContentUrl(contentUrl)).toThrow(
      "content URL",
    );
  });

  it("adds attachment disposition while preserving existing query parameters", () => {
    expect(
      withAttachmentDisposition(
        "/api/v1/assets/asset-1/content?preview=1",
      ),
    ).toBe(
      "/api/v1/assets/asset-1/content?preview=1&disposition=attachment",
    );
  });

  it("classifies AbortError separately", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new DOMException("aborted", "AbortError"));
    const api = createMaterialsAgentApi({ fetchImpl });

    await expect(api.getTimelinePage(
      "conversation-1",
      50,
      undefined,
      new AbortController().signal,
    )).rejects.toBeInstanceOf(RequestAbortedError);
  });

  it("never exposes raw HTML or stack text through user-visible errors", () => {
    const protocol = new ProtocolResponseError(
      "<html>private raw body</html>",
    );
    protocol.stack = "private stack";

    const visible = toUserVisibleError(protocol);

    expect(visible.message).toBe("服务返回了无法识别的响应。");
    expect(JSON.stringify(visible)).not.toContain("private raw body");
    expect(JSON.stringify(visible)).not.toContain("private stack");
  });
});
