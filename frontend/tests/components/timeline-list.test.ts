import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import TimelineList from "../../src/components/TimelineList.vue";
import type {
  TimelineAssistantMessageItem,
  TimelineItem,
  TimelineToolTaskItem,
  TimelineUserMessageItem,
} from "../../src/api/types";

function userItem(
  id: string,
  content: string,
  anchorAt: string,
): TimelineUserMessageItem {
  return {
    item_type: "USER_MESSAGE",
    item_id: id,
    task_id: `task-${id}`,
    anchor_at: anchorAt,
    message: {
      message_id: id,
      role: "USER",
      content_text: content,
      created_at: anchorAt,
    },
  };
}

function assistantItem(
  id: string,
  content: string,
  anchorAt: string,
): TimelineAssistantMessageItem {
  return {
    item_type: "ASSISTANT_MESSAGE",
    item_id: id,
    task_id: `task-${id}`,
    anchor_at: anchorAt,
    message: {
      message_id: id,
      role: "ASSISTANT",
      content_text: content,
      created_at: anchorAt,
    },
  };
}

function toolItem(id: string): TimelineToolTaskItem {
  const timestamp = "2026-07-24T12:00:00Z";
  return {
    item_type: "TOOL_TASK",
    item_id: id,
    task_id: id,
    anchor_at: timestamp,
    initial_user_message: {
      message_id: `message-${id}`,
      role: "USER",
      content_text: "请执行材料工具",
      created_at: timestamp,
    },
    task: {
      task_id: id,
      task_type: "TOOL_EXECUTION",
      status: "RUNNING",
      selected_tool_run_id: null,
      selected_result_id: null,
      created_at: timestamp,
      started_at: timestamp,
      updated_at: timestamp,
      completed_at: null,
      error_code: null,
      safe_error_message: null,
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
    needs_input: null,
    errors: [],
  };
}

function mountTimeline(items: TimelineItem[]) {
  return mount(TimelineList, {
    props: {
      items,
      conversationId: "conversation-1",
      mutationBusy: false,
      taskDetailsById: {},
      taskDetailsLoadingById: {},
    },
  });
}

describe("TimelineList", () => {
  it("renders all three discriminated item types with the correct component", () => {
    const wrapper = mountTimeline([
      userItem("user-1", "用户消息", "2026-07-24T10:00:00Z"),
      assistantItem(
        "assistant-1",
        "知识回答",
        "2026-07-24T10:01:00Z",
      ),
      toolItem("task-1"),
    ]);

    expect(wrapper.findComponent({ name: "UserMessageItem" }).exists()).toBe(
      true,
    );
    expect(
      wrapper.findComponent({ name: "AssistantMessageItem" }).exists(),
    ).toBe(true);
    expect(wrapper.findComponent({ name: "ToolTaskCard" }).exists()).toBe(
      true,
    );
  });

  it("keeps the Backend array order when timestamps disagree", () => {
    const wrapper = mountTimeline([
      assistantItem(
        "assistant-late",
        "第一项",
        "2026-07-24T18:00:00Z",
      ),
      userItem("user-early", "第二项", "2026-07-24T08:00:00Z"),
      toolItem("task-middle"),
    ]);

    expect(
      wrapper
        .findAll("[data-timeline-item]")
        .map((element) => element.attributes("data-timeline-item")),
    ).toEqual([
      "ASSISTANT_MESSAGE:assistant-late",
      "USER_MESSAGE:user-early",
      "TOOL_TASK:task-middle",
    ]);
  });

  it("does not duplicate a Tool initial message as a top-level user item", () => {
    const wrapper = mountTimeline([toolItem("task-1")]);

    expect(wrapper.findAllComponents({ name: "UserMessageItem" })).toHaveLength(
      0,
    );
    expect(wrapper.text().match(/请执行材料工具/g)).toHaveLength(1);
  });

  it("shows a safe empty state", () => {
    const wrapper = mountTimeline([]);

    expect(wrapper.text()).toContain("当前对话还没有消息");
    expect(wrapper.find("[data-timeline-item]").exists()).toBe(false);
  });

  it("renders external message text without executing HTML", () => {
    const dangerous = '<img src=x onerror="window.__unsafe = true">';
    const wrapper = mountTimeline([
      userItem("user-safe", dangerous, "not-a-date"),
      assistantItem("assistant-safe", "<script>unsafe()</script>", ""),
    ]);

    expect(wrapper.text()).toContain(dangerous);
    expect(wrapper.text()).toContain("<script>unsafe()</script>");
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.find("script").exists()).toBe(false);
  });
});
