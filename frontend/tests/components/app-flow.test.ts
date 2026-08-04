import { flushPromises, mount } from "@vue/test-utils";
import { nextTick, ref } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ConversationListItem,
  TimelineAssistantMessageItem,
  TimelineToolTaskItem,
  TimelineUserMessageItem,
} from "../../src/api/types";
import type { UserVisibleError } from "../../src/api/errors";
import type {
  MutationStatus,
  PendingMutationV1,
} from "../../src/composables/useIdempotentRequest";
import type {
  MaterialsAgentState,
  SupplementTarget,
} from "../../src/composables/useMaterialsAgent";

const moduleMocks = vi.hoisted(() => ({
  useMaterialsAgent: vi.fn(),
}));

vi.mock("../../src/composables/useMaterialsAgent", () => ({
  useMaterialsAgent: moduleMocks.useMaterialsAgent,
}));

import App from "../../src/App.vue";

const timestamp = "2026-07-24T12:00:00Z";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function userItem(content = "用户问题"): TimelineUserMessageItem {
  return {
    item_type: "USER_MESSAGE",
    item_id: "message-user",
    task_id: "task-knowledge",
    anchor_at: timestamp,
    message: {
      message_id: "message-user",
      role: "USER",
      content_text: content,
      created_at: timestamp,
    },
  };
}

function assistantItem(content = "知识回答"): TimelineAssistantMessageItem {
  return {
    item_type: "ASSISTANT_MESSAGE",
    item_id: "message-assistant",
    task_id: "task-knowledge",
    anchor_at: timestamp,
    message: {
      message_id: "message-assistant",
      role: "ASSISTANT",
      content_text: content,
      created_at: timestamp,
    },
  };
}

function toolItem(
  status: TimelineToolTaskItem["task"]["status"] = "NEEDS_INPUT",
  taskId = "task-tool",
): TimelineToolTaskItem {
  const hasResult =
    status === "SUCCEEDED" || status === "PARTIALLY_SUCCEEDED";
  return {
    item_type: "TOOL_TASK",
    item_id: taskId,
    task_id: taskId,
    anchor_at: timestamp,
    initial_user_message: {
      message_id: "message-tool",
      role: "USER",
      content_text: "执行材料工具",
      created_at: timestamp,
    },
    task: {
      task_id: taskId,
      task_type: "TOOL_EXECUTION",
      status,
      selected_tool_run_id: hasResult ? "run-1" : null,
      selected_result_id: hasResult ? "result-1" : null,
      created_at: timestamp,
      started_at: null,
      updated_at: timestamp,
      completed_at: null,
      error_code: null,
      safe_error_message: null,
    },
    input_thread: [],
    input_thread_count: 0,
    input_thread_truncated: false,
    tool_runs: {
      attempt_count: hasResult ? 1 : 0,
      selected_tool_run: null,
      has_history: hasResult,
    },
    result: hasResult
      ? {
          result_id: "result-1",
          tool_run_id: "run-1",
          status:
            status === "PARTIALLY_SUCCEEDED"
              ? "PARTIALLY_SUCCEEDED"
              : "SUCCEEDED",
          requested_outputs: ["sem_image"],
          completed_outputs: ["sem_image"],
          failed_outputs:
            status === "PARTIALLY_SUCCEEDED"
              ? ["mechanical_properties"]
              : [],
          data: { value: 1 },
          warnings: [],
          provenance: { task_id: taskId },
          error: null,
          tool_id: "zta35g_sem_virtual_lab",
          tool_version: "0.1.0",
          schema_version: "1.0",
          created_at: timestamp,
        }
      : null,
    assets: [],
    explanation: hasResult ? null : null,
    latest_explanation_failure:
      status === "PARTIALLY_SUCCEEDED"
        ? {
            explanation_id: "explanation-2",
            attempt_no: 2,
            status: "FAILED",
            language: "zh-CN",
            text: null,
            completed_at: timestamp,
            duration_ms: 20,
            error_code: "EXPLANATION_FAILED",
            safe_error_message: "解释失败。",
          }
        : null,
    needs_input:
      status === "NEEDS_INPUT"
        ? {
            missing_fields: ["aging_temperature"],
            ambiguous_fields: [],
            normalized_input: null,
          }
        : null,
    errors: [],
  };
}

interface MutableAgentRefs {
  conversations: ReturnType<typeof ref<ConversationListItem[]>>;
  conversationNextCursor: ReturnType<typeof ref<string | null>>;
  selectedConversationId: ReturnType<typeof ref<string | null>>;
  timeline: ReturnType<
    typeof ref<
      Array<
        | TimelineUserMessageItem
        | TimelineAssistantMessageItem
        | TimelineToolTaskItem
      >
    >
  >;
  timelineLoading: ReturnType<typeof ref<boolean>>;
  conversationListLoading: ReturnType<typeof ref<boolean>>;
  taskDetailsById: ReturnType<
    typeof ref<MaterialsAgentState["taskDetailsById"]["value"]>
  >;
  taskDetailsLoadingById: ReturnType<
    typeof ref<MaterialsAgentState["taskDetailsLoadingById"]["value"]>
  >;
  supplementTarget: ReturnType<typeof ref<SupplementTarget | null>>;
  pendingMutation: ReturnType<typeof ref<PendingMutationV1 | null>>;
  mutationStatus: ReturnType<typeof ref<MutationStatus>>;
  conversationCreationUncertain: ReturnType<typeof ref<boolean>>;
  globalError: ReturnType<typeof ref<UserVisibleError | null>>;
  globalErrors: ReturnType<typeof ref<UserVisibleError[]>>;
  lastRequestId: ReturnType<typeof ref<string | null>>;
}

let refs: MutableAgentRefs;
let agent: MaterialsAgentState;

function buildAgent(): MaterialsAgentState {
  refs = {
    conversations: ref([
      {
        conversation_id: "conversation-1",
        title: "材料讨论",
        created_at: timestamp,
        updated_at: timestamp,
        last_activity_preview: "上一条消息",
      },
    ]),
    conversationNextCursor: ref("next-page"),
    selectedConversationId: ref("conversation-1"),
    timeline: ref([userItem(), assistantItem()]),
    timelineLoading: ref(false),
    conversationListLoading: ref(false),
    taskDetailsById: ref({}),
    taskDetailsLoadingById: ref({}),
    supplementTarget: ref(null),
    pendingMutation: ref(null),
    mutationStatus: ref("IDLE"),
    conversationCreationUncertain: ref(false),
    globalError: ref(null),
    globalErrors: ref([]),
    lastRequestId: ref(null),
  };

  const setSupplementTarget = vi.fn((target: SupplementTarget) => {
    refs.supplementTarget.value = { ...target };
  });
  const cancelSupplementTarget = vi.fn(() => {
    refs.supplementTarget.value = null;
  });
  const discardPendingMutation = vi.fn(() => {
    const discarded = refs.pendingMutation.value !== null;
    refs.pendingMutation.value = null;
    refs.mutationStatus.value = "IDLE";
    return { discarded };
  });

  return {
    ...refs,
    initialize: vi.fn(() => Promise.resolve()),
    loadConversations: vi.fn(() => Promise.resolve()),
    refreshConversations: vi.fn(() => Promise.resolve()),
    loadMoreConversations: vi.fn(() => Promise.resolve()),
    createConversation: vi.fn(() =>
      Promise.resolve({
        conversation_id: "conversation-created",
        title: null,
        created_at: timestamp,
        updated_at: timestamp,
      }),
    ),
    selectConversation: vi.fn((conversationId: string) => {
      refs.selectedConversationId.value = conversationId;
      return Promise.resolve();
    }),
    refreshTimeline: vi.fn(() => Promise.resolve()),
    loadTaskHistory: vi.fn(() => Promise.reject(new Error("not configured"))),
    setSupplementTarget,
    cancelSupplementTarget,
    submitNewTask: vi.fn(() => Promise.resolve()),
    submitSupplement: vi.fn(() => Promise.resolve()),
    retryTool: vi.fn(() => Promise.resolve()),
    retryExplanation: vi.fn(() => Promise.resolve()),
    retryPendingMutation: vi.fn(() => Promise.resolve()),
    discardPendingMutation,
    startPolling: vi.fn(),
    stopPolling: vi.fn(),
  } as MaterialsAgentState;
}

beforeEach(() => {
  vi.clearAllMocks();
  agent = buildAgent();
  moduleMocks.useMaterialsAgent.mockReturnValue(agent);
});

describe("App flow", () => {
  it("creates one application state, initializes, then starts polling once", async () => {
    const wrapper = mount(App);
    await flushPromises();

    expect(moduleMocks.useMaterialsAgent).toHaveBeenCalledTimes(1);
    expect(agent.initialize).toHaveBeenCalledTimes(1);
    expect(agent.startPolling).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain("材料讨论");
    expect(wrapper.text()).toContain("用户问题");
    expect(wrapper.text()).toContain("知识回答");

    wrapper.unmount();
    await flushPromises();
    expect(agent.startPolling).toHaveBeenCalledTimes(1);
    expect(agent.stopPolling).not.toHaveBeenCalled();
  });

  it("keeps a basic page and safe error visible when initialize fails", async () => {
    refs.globalError.value = { message: "初始化读取失败。" };
    vi.mocked(agent.initialize).mockRejectedValueOnce(new Error("private"));
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("高端金属材料组织图像智能体");
    expect(wrapper.text()).toContain("初始化读取失败");
    expect(wrapper.text()).not.toContain("private");
    expect(agent.startPolling).toHaveBeenCalledTimes(1);
  });

  it("maps Conversation create, select, and load-more events", async () => {
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.get("[data-action=refresh-conversations]").trigger("click");
    expect(agent.refreshConversations).toHaveBeenCalledTimes(1);

    await wrapper.get("[data-action=create-conversation]").trigger("click");
    await flushPromises();
    expect(agent.createConversation).toHaveBeenCalledTimes(1);

    await wrapper.get("[data-conversation-id]").trigger("click");
    await flushPromises();
    expect(agent.selectConversation).toHaveBeenCalledWith("conversation-1");

    await wrapper.get("[data-action=load-more]").trigger("click");
    expect(agent.loadMoreConversations).toHaveBeenCalledTimes(1);
  });

  it("maps new task, manual refresh, and explicit supplement flow", async () => {
    refs.timeline.value = [toolItem("NEEDS_INPUT")];
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.get("[data-action=supplement]").trigger("click");
    await nextTick();
    expect(agent.setSupplementTarget).toHaveBeenCalledWith({
      conversationId: "conversation-1",
      taskId: "task-tool",
      summary: "待补充：时效温度",
    });
    expect(wrapper.text()).toContain("正在为指定任务补充信息");

    await wrapper.get("[data-action=cancel-supplement]").trigger("click");
    expect(agent.cancelSupplementTarget).toHaveBeenCalledTimes(1);

    agent.setSupplementTarget({
      conversationId: "conversation-1",
      taskId: "task-tool",
      summary: "待补充",
    });
    await nextTick();
    await wrapper.get("textarea").setValue("补充内容");
    await wrapper.get("form").trigger("submit");
    expect(agent.submitSupplement).toHaveBeenCalledWith("补充内容");

    refs.supplementTarget.value = null;
    refs.mutationStatus.value = "BUSINESS_FAILED";
    await nextTick();
    await wrapper.get("textarea").setValue("新的知识问题");
    await wrapper.get("form").trigger("submit");
    expect(agent.submitNewTask).toHaveBeenCalledWith("新的知识问题");

    await wrapper.get("[data-action=refresh-timeline]").trigger("click");
    expect(agent.refreshTimeline).toHaveBeenCalledTimes(1);
  });

  it("maps Tool and Explanation retries without exposing Task history", async () => {
    refs.timeline.value = [toolItem("PARTIALLY_SUCCEEDED")];
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.get("[data-action=retry-tool]").trigger("click");
    await wrapper.get("[data-action=retry-explanation]").trigger("click");

    expect(agent.retryTool).toHaveBeenCalledWith("task-tool");
    expect(agent.retryExplanation).toHaveBeenCalledWith("result-1");
    expect(wrapper.find("[data-action=load-history]").exists()).toBe(false);
    expect(agent.loadTaskHistory).not.toHaveBeenCalled();
  });

  it("shows safe global error details without raw objects", async () => {
    refs.globalError.value = {
      message: "请求冲突。",
      status: 409,
      request_id: "request-public",
      details: [
        {
          field: "target_task_id",
          code: "TARGET_TASK_NOT_RECOVERABLE",
          message: "该任务当前不能补充。",
        },
      ],
    };
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.get("[role=alert]").text()).toContain("请求冲突");
    expect(wrapper.get("[role=alert]").text()).toContain("HTTP 409");
    expect(wrapper.get("[role=alert]").text()).toContain("request-public");
    expect(wrapper.get("[role=alert]").text()).toContain("该任务当前不能补充");
    expect(wrapper.text()).not.toContain("[object Object]");
  });

  it("renders deduplicated action and read notices without raw diagnostics", async () => {
    refs.globalErrors.value = [
      {
        message:
          "无法确认 Conversation 是否已创建，请先刷新 Conversation 列表，避免重复创建。",
      },
      {
        message: "任务状态已变化",
        status: 409,
        request_id: "request-conflict",
      },
      {
        message: "读取失败，请检查网络后重试。",
      },
      {
        message: "任务状态已变化",
        status: 409,
        request_id: "request-conflict",
      },
    ];
    const wrapper = mount(App);
    await flushPromises();

    const notices = wrapper.findAll(
      ".global-notice.global-notice--error[role=alert]",
    );
    expect(notices).toHaveLength(3);
    expect(wrapper.text()).toContain(
      "无法确认 Conversation 是否已创建，请先刷新 Conversation 列表，避免重复创建。",
    );
    expect(wrapper.text()).toContain("任务状态已变化");
    expect(wrapper.text()).toContain("读取失败，请检查网络后重试。");
    expect(wrapper.text()).not.toContain("raw body");
    expect(wrapper.text()).not.toContain("stack");
    expect(wrapper.text()).not.toContain("Idempotency-Key");
  });

  it("offers original-request recovery and local discard without showing the key", async () => {
    refs.mutationStatus.value = "UNCERTAIN";
    refs.pendingMutation.value = {
      version: 1,
      operation: "TASK_CREATE",
      resourceId: "conversation-1",
      idempotencyKey: "secret-idempotency-key",
      createdAt: timestamp,
      body: {
        submission_mode: "NEW_TASK",
        content_text: "不确定请求",
        target_task_id: null,
      },
    };
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("未能确认上一次操作结果");
    expect(wrapper.text()).toContain(
      "放弃恢复记录不会取消服务器端可能已经完成的操作",
    );
    expect(wrapper.text()).not.toContain("secret-idempotency-key");

    await wrapper.get("[data-action=retry-pending]").trigger("click");
    expect(agent.retryPendingMutation).toHaveBeenCalledTimes(1);
    await wrapper.get("[data-action=discard-pending]").trigger("click");
    expect(agent.discardPendingMutation).toHaveBeenCalledTimes(1);
  });

  it("keeps list refresh available while Conversation creation uncertainty disables only create", async () => {
    refs.conversationCreationUncertain.value = true;
    const wrapper = mount(App);
    await flushPromises();

    expect(
      wrapper.get("[data-action=create-conversation]").attributes(),
    ).toHaveProperty("disabled");
    expect(
      wrapper.get("[data-action=refresh-conversations]").attributes(),
    ).not.toHaveProperty("disabled");
    expect(wrapper.get("textarea").attributes()).not.toHaveProperty(
      "disabled",
    );

    await wrapper.get("[data-action=refresh-conversations]").trigger("click");
    expect(agent.refreshConversations).toHaveBeenCalledTimes(1);
    expect(agent.createConversation).not.toHaveBeenCalled();
  });

  it("clears an ordinary draft when switching to another Conversation", async () => {
    refs.conversations.value = [
      {
        conversation_id: "conversation-1",
        title: "材料讨论",
        created_at: timestamp,
        updated_at: timestamp,
        last_activity_preview: "上一条消息",
      },
      {
        conversation_id: "conversation-2",
        title: "第二个对话",
        created_at: timestamp,
        updated_at: timestamp,
        last_activity_preview: null,
      },
    ];
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get("textarea").setValue("只属于 Conversation A");

    refs.selectedConversationId.value = "conversation-2";
    await nextTick();

    expect(
      (wrapper.get("textarea").element as HTMLTextAreaElement).value,
    ).toBe("");
  });

  it("does not submit Conversation A supplement draft as a new task in B", async () => {
    refs.timeline.value = [toolItem("NEEDS_INPUT", "task-needs-input")];
    refs.conversations.value = [
      {
        conversation_id: "conversation-1",
        title: "材料讨论",
        created_at: timestamp,
        updated_at: timestamp,
        last_activity_preview: "上一条消息",
      },
      {
        conversation_id: "conversation-2",
        title: "第二个对话",
        created_at: timestamp,
        updated_at: timestamp,
        last_activity_preview: null,
      },
    ];
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get("[data-action=supplement]").trigger("click");
    await nextTick();
    await wrapper.get("textarea").setValue("Conversation A 的补参草稿");

    refs.selectedConversationId.value = "conversation-2";
    refs.supplementTarget.value = null;
    refs.timeline.value = [];
    await nextTick();
    await wrapper.get("form").trigger("submit");

    expect(
      (wrapper.get("textarea").element as HTMLTextAreaElement).value,
    ).toBe("");
    expect(agent.submitNewTask).not.toHaveBeenCalled();
    expect(agent.submitSupplement).not.toHaveBeenCalled();
  });

  it("disables every write entry while Conversation creation POST is pending", async () => {
    refs.timeline.value = [
      toolItem("NEEDS_INPUT", "task-needs-input"),
      toolItem("PARTIALLY_SUCCEEDED", "task-retry"),
    ];
    const pendingCreate = deferred<
      Awaited<ReturnType<MaterialsAgentState["createConversation"]>>
    >();
    vi.mocked(agent.createConversation).mockReturnValueOnce(
      pendingCreate.promise,
    );
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.get("[data-action=create-conversation]").trigger("click");
    await nextTick();

    expect(wrapper.get("textarea").attributes()).toHaveProperty("disabled");
    expect(
      wrapper.get("[data-action=supplement]").attributes(),
    ).toHaveProperty("disabled");
    expect(
      wrapper.get("[data-action=retry-tool]").attributes(),
    ).toHaveProperty("disabled");
    expect(
      wrapper.get("[data-action=retry-explanation]").attributes(),
    ).toHaveProperty("disabled");
    expect(
      wrapper.get("[data-action=refresh-timeline]").attributes(),
    ).not.toHaveProperty("disabled");
    expect(
      wrapper.get("[data-action=refresh-conversations]").attributes(),
    ).not.toHaveProperty("disabled");
    expect(
      wrapper.get("[data-conversation-id]").attributes(),
    ).not.toHaveProperty("disabled");
    await wrapper.get("[data-action=create-conversation]").trigger("click");
    await wrapper.get("form").trigger("submit");
    await wrapper.get("[data-action=supplement]").trigger("click");
    await wrapper.get("[data-action=retry-tool]").trigger("click");
    await wrapper.get("[data-action=retry-explanation]").trigger("click");
    expect(agent.submitNewTask).not.toHaveBeenCalled();
    expect(agent.setSupplementTarget).not.toHaveBeenCalled();
    expect(agent.retryTool).not.toHaveBeenCalled();
    expect(agent.retryExplanation).not.toHaveBeenCalled();
    expect(agent.createConversation).toHaveBeenCalledTimes(1);

    pendingCreate.resolve({
      conversation_id: "conversation-created",
      title: null,
      created_at: timestamp,
      updated_at: timestamp,
    });
    await flushPromises();
  });

  it.each<MutationStatus>(["SENDING", "UNCERTAIN"])(
    "disables all new writes while mutation status is %s",
    async (status) => {
      refs.mutationStatus.value = status;
      refs.timeline.value = [toolItem("PARTIALLY_SUCCEEDED")];
      if (status === "UNCERTAIN") {
        refs.pendingMutation.value = {
          version: 1,
          operation: "TOOL_RETRY",
          resourceId: "task-tool",
          idempotencyKey: "hidden-key",
          createdAt: timestamp,
          body: { reason: "USER_REQUESTED_RETRY" },
        };
      }
      const wrapper = mount(App);
      await flushPromises();

      expect(
        wrapper.get("[data-action=create-conversation]").attributes(),
      ).toHaveProperty("disabled");
      expect(wrapper.get("textarea").attributes()).toHaveProperty("disabled");
      expect(
        wrapper.get("[data-action=retry-tool]").attributes(),
      ).toHaveProperty("disabled");
      expect(
        wrapper.get("[data-action=retry-explanation]").attributes(),
      ).toHaveProperty("disabled");
    },
  );

  it("delegates every action to the composable and never calls fetch directly", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    refs.timeline.value = [toolItem("PARTIALLY_SUCCEEDED")];
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.get("[data-action=retry-tool]").trigger("click");
    await wrapper.get("[data-action=retry-explanation]").trigger("click");
    await wrapper.get("[data-action=refresh-timeline]").trigger("click");
    await flushPromises();

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(agent.retryTool).toHaveBeenCalledTimes(1);
    expect(agent.retryExplanation).toHaveBeenCalledTimes(1);
    expect(agent.refreshTimeline).toHaveBeenCalled();
  });
});
