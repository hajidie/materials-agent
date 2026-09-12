import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import ChatComposer from "../../src/components/ChatComposer.vue";

function composer() { return mount(ChatComposer, { props: { disabled: false, sending: false, completed: 0, waitingQuestion: null } }); }
describe("Agent composer", () => {
  it("validates input and submits a goal", async () => {
    const wrapper = composer();
    await wrapper.get("form").trigger("submit");
    expect(wrapper.find('[role="alert"]').exists()).toBe(true);
    await wrapper.get("textarea").setValue("1000 MPa 转 GPa");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("submit")).toEqual([["1000 MPa 转 GPa"]]);
  });
  it("preserves uncertain input and clears only after committed success", async () => {
    const wrapper = composer();
    await wrapper.get("textarea").setValue("目标");
    await wrapper.setProps({ disabled: true, sending: true });
    expect(wrapper.get("textarea").element.value).toBe("目标");
    await wrapper.setProps({ disabled: false, sending: false });
    expect(wrapper.get("textarea").element.value).toBe("目标");
    await wrapper.setProps({ completed: 1 });
    expect(wrapper.get("textarea").element.value).toBe("");
  });
  it("does not submit during Chinese IME composition", async () => {
    const wrapper = composer();
    await wrapper.get("textarea").setValue("材料");
    await wrapper.get("textarea").trigger("keydown", { key: "Enter", isComposing: true });
    expect(wrapper.emitted("submit")).toBeUndefined();
    await wrapper.get("textarea").trigger("keydown", { key: "Enter", isComposing: false });
    expect(wrapper.emitted("submit")).toHaveLength(1);
  });
  it("shows the exact waiting question and offers a new goal", async () => {
    const wrapper = composer();
    await wrapper.setProps({ waitingQuestion: "请补充 value" });
    expect(wrapper.text()).toContain("请补充 value");
    await wrapper.get(".supplement-banner button").trigger("click");
    expect(wrapper.emitted("cancel-resume")).toHaveLength(1);
  });
});
