import { describe, expect, expectTypeOf, it, vi } from "vitest";

import {
  createMaterialsAgentApi,
  withAttachmentDisposition,
} from "../../src/api/client";
import {
  ConversationCreationUncertaintyError,
  ProtocolResponseError,
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
