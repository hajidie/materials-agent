import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ChatComposer from "../../src/components/ChatComposer.vue";
import type { MutationStatus } from "../../src/composables/useIdempotentRequest";
import type { SupplementTarget } from "../../src/composables/useMaterialsAgent";

function mountComposer(
  overrides: Partial<{
    disabled: boolean;
    sending: boolean;
    supplementTarget: SupplementTarget | null;
    mutationStatus: MutationStatus;
  }> = {},
) {
  return mount(ChatComposer, {
    props: {
      disabled: false,
      sending: false,
      supplementTarget: null,
      mutationStatus: "IDLE",
      ...overrides,
    },
  });
}

describe("ChatComposer", () => {
  it("rejects blank input before emitting", async () => {
    const wrapper = mountComposer();
    await wrapper.get("textarea").setValue(" \n ");

    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit-new-task")).toBeUndefined();
    expect(wrapper.text()).toContain("请输入消息");
  });

  it("emits a trimmed NEW_TASK submission in ordinary mode", async () => {
    const wrapper = mountComposer();
    await wrapper.get("textarea").setValue("  请解释 ZTA35G  ");

    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit-new-task")).toEqual([
      ["请解释 ZTA35G"],
    ]);
    expect(wrapper.emitted("submit-supplement")).toBeUndefined();
  });

  it("locks supplement mode to the explicit target and can cancel it", async () => {
    const target: SupplementTarget = {
      conversationId: "conversation-1",
      taskId: "task-explicit",
      summary: "缺少时效温度",
    };
    const wrapper = mountComposer({ supplementTarget: target });
    await wrapper.get("textarea").setValue("时效温度为 120°C");

    expect(wrapper.text()).toContain("正在为指定任务补充信息");
    expect(wrapper.text()).toContain("缺少时效温度");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("submit-supplement")).toEqual([
      ["时效温度为 120°C"],
    ]);
    expect(wrapper.emitted("submit-new-task")).toBeUndefined();

    await wrapper.get("[data-action=cancel-supplement]").trigger("click");
    expect(wrapper.emitted("cancel-supplement")).toHaveLength(1);
    expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe(
      "时效温度为 120°C",
    );
  });

  it("submits on Enter and keeps Shift+Enter as a newline", async () => {
    const wrapper = mountComposer();
    const textarea = wrapper.get("textarea");
    await textarea.setValue("第一行");

    await textarea.trigger("keydown", {
      key: "Enter",
      shiftKey: true,
    });
    expect(wrapper.emitted("submit-new-task")).toBeUndefined();

    await textarea.trigger("keydown", {
      key: "Enter",
      shiftKey: false,
    });
    expect(wrapper.emitted("submit-new-task")).toEqual([["第一行"]]);
  });

  it.each([
    { disabled: true, sending: false, label: "disabled" },
    { disabled: false, sending: true, label: "sending" },
  ])("blocks submission while $label", async ({ disabled, sending }) => {
    const wrapper = mountComposer({ disabled, sending });
    await wrapper.get("textarea").setValue("不能发送");

    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit-new-task")).toBeUndefined();
    expect(wrapper.get("textarea").attributes()).toHaveProperty("disabled");
    expect(
      wrapper.get("button[type=submit]").attributes(),
    ).toHaveProperty("disabled");
  });

  it("blocks immediate duplicate submission before parent state updates", async () => {
    const wrapper = mountComposer();
    await wrapper.get("textarea").setValue("只发送一次");

    await wrapper.get("form").trigger("submit");
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit-new-task")).toHaveLength(1);
  });

  it("clears the draft only after an explicit successful mutation", async () => {
    const wrapper = mountComposer();
    await wrapper.get("textarea").setValue("成功后清空");
    await wrapper.get("form").trigger("submit");

    expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe(
      "成功后清空",
    );
    await wrapper.setProps({
      sending: true,
      mutationStatus: "SENDING",
    });
    await wrapper.setProps({
      sending: false,
      mutationStatus: "SUCCEEDED",
    });
    expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe(
      "",
    );
  });

  it.each<MutationStatus>(["BUSINESS_FAILED", "UNCERTAIN"])(
    "keeps the draft when mutation finishes as %s",
    async (mutationStatus) => {
      const wrapper = mountComposer();
      await wrapper.get("textarea").setValue("失败时保留");
      await wrapper.get("form").trigger("submit");
      await wrapper.setProps({
        sending: false,
        mutationStatus,
      });

      expect(
        (wrapper.get("textarea").element as HTMLTextAreaElement).value,
      ).toBe("失败时保留");
    },
  );
});
