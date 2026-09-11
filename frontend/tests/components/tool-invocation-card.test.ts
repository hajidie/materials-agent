import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ToolInvocationCard from "../../src/components/ToolInvocationCard.vue";
import type { ToolInvocation } from "../../src/api/types";

function invocation(
  overrides: Partial<ToolInvocation> = {},
): ToolInvocation {
  return {
    invocation_run_id: "invocation-1",
    conversation_id: "conversation-1",
    source_message_id: "message-1",
    task_id: null,
    status: "PENDING_CONFIRMATION",
    tool: {
      tool_id: "dev_fake_side_effect",
      version: "1",
      display_name: "Fake Side-effect Tool",
      execution_profile: "SIDE_EFFECT",
      confirmation_required: true,
      confirmation_prompt: "确认写入开发 Fake Sink。",
    },
    confirmation_required: true,
    confirmation_expires_at: "2026-09-04T10:15:00Z",
    confirmed_at: null,
    rejected_at: null,
    expired_at: null,
    dispatch_started_at: null,
    error_code: null,
    safe_error_message: null,
    result: null,
    created_at: "2026-09-04T10:00:00Z",
    updated_at: "2026-09-04T10:00:00Z",
    completed_at: null,
    ...overrides,
  };
}

describe("ToolInvocationCard", () => {
  it("renders the safe confirmation projection and emits one explicit action", async () => {
    const wrapper = mount(ToolInvocationCard, {
      props: { invocation: invocation(), mutationBusy: false },
    });

    expect(wrapper.text()).toContain("确认写入开发 Fake Sink");
    await wrapper.get('[data-action="confirm-tool-invocation"]').trigger("click");
    await wrapper.get('[data-action="reject-tool-invocation"]').trigger("click");

    expect(wrapper.emitted("confirm")).toEqual([["invocation-1"]]);
    expect(wrapper.emitted("reject")).toEqual([["invocation-1"]]);
  });

  it("restores terminal presentation without rendering confirmation controls", () => {
    const wrapper = mount(ToolInvocationCard, {
      props: {
        invocation: invocation({
          status: "SUCCEEDED",
          confirmed_at: "2026-09-04T10:01:00Z",
          dispatch_started_at: "2026-09-04T10:01:01Z",
          completed_at: "2026-09-04T10:01:02Z",
          result: {
            data: { accepted: true },
            presentation: {
              title: "开发测试副作用已确认",
              summary: "Fake sink 已按幂等键记录一次。",
            },
          },
        }),
        mutationBusy: false,
      },
    });

    expect(wrapper.text()).toContain("Fake sink 已按幂等键记录一次");
    expect(wrapper.find('[data-action="confirm-tool-invocation"]').exists()).toBe(false);
  });
});
