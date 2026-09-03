import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { afterEach, describe, expect, it } from "vitest";

import DeleteConversationDialog from "../../src/components/DeleteConversationDialog.vue";


afterEach(() => {
  document.body.innerHTML = "";
});


describe("DeleteConversationDialog", () => {
  it("starts on cancel and supports Escape cancellation", async () => {
    const wrapper = mount(DeleteConversationDialog, {
      attachTo: document.body,
      props: { title: "材料讨论", pending: false, error: null },
    });
    await nextTick();

    expect(wrapper.get("[role=alertdialog]").attributes("role")).toBe(
      "alertdialog",
    );
    expect(document.activeElement?.textContent?.trim()).toBe("取消");
    await wrapper.get("[role=alertdialog]").trigger("keydown", { key: "Escape" });
    expect(wrapper.emitted("cancel")).toHaveLength(1);
  });

  it("prevents duplicate actions and exposes an inline error while pending", async () => {
    const wrapper = mount(DeleteConversationDialog, {
      props: {
        title: "材料讨论",
        pending: true,
        error: "对话仍有执行中的任务，请稍后重试。",
      },
    });

    expect(wrapper.get("[role=alert]").text()).toContain("稍后重试");
    expect(
      wrapper
        .findAll("button")
        .every((button) => button.attributes("disabled") !== undefined),
    ).toBe(true);
    await wrapper.get("[role=alertdialog]").trigger("keydown", { key: "Escape" });
    expect(wrapper.emitted("cancel")).toBeUndefined();
  });
});
