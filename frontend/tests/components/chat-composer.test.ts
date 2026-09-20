import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import ChatComposer from "../../src/components/ChatComposer.vue";

function composer() { return mount(ChatComposer, { props: { disabled: false, sending: false, completed: 0, waitingQuestion: null } }); }
describe("Agent composer", () => {
  it("omits helper copy while keeping validation and keyboard behavior", async () => {
    const wrapper = composer();
    expect(wrapper.get("label").classes()).toContain("visually-hidden");
    expect(wrapper.get("textarea").attributes("rows")).toBe("1");
    expect(wrapper.text()).not.toContain("单张正方形");
    expect(wrapper.text()).not.toContain("Shift+Enter");
    expect(wrapper.get("textarea").attributes("aria-describedby")).toBeUndefined();
    expect(wrapper.get('input[type="file"]').attributes("aria-describedby")).toBeUndefined();
    await wrapper.get("textarea").trigger("keydown", { key: "Enter" });
    expect(wrapper.get("textarea").attributes("aria-describedby")).toBe("composer-error");
    expect(wrapper.get("#composer-error").text()).toContain("请输入消息");
    await wrapper.get("textarea").setValue("研究目标");
    await wrapper.get("textarea").trigger("keydown", { key: "Enter", shiftKey: true });
    expect(wrapper.emitted("submit")).toBeUndefined();
  });
  it("validates input and submits a goal", async () => {
    const wrapper = composer();
    await wrapper.get("form").trigger("submit");
    expect(wrapper.find('[role="alert"]').exists()).toBe(true);
    await wrapper.get("textarea").setValue("1000 MPa 转 GPa");
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("submit")).toEqual([["1000 MPa 转 GPa"]]);
  });
  it("hides a sent draft, restores it after uncertainty, and clears it after success", async () => {
    const wrapper = composer();
    await wrapper.get("textarea").setValue("目标");
    await wrapper.setProps({ disabled: true, sending: true, initialDraft: "目标" });
    expect(wrapper.get("textarea").element.value).toBe("");
    await wrapper.setProps({ disabled: false, sending: false });
    expect(wrapper.get("textarea").element.value).toBe("目标");
    await wrapper.setProps({ completed: 1 });
    expect(wrapper.get("textarea").element.value).toBe("");
  });
  it("grows with content and switches to internal scrolling at its height limit", async () => {
    const wrapper = composer();
    const textarea = wrapper.get("textarea");
    Object.defineProperty(textarea.element, "scrollHeight", { configurable: true, value: 96 });
    await textarea.setValue("第一行\n第二行\n第三行");
    expect((textarea.element as HTMLTextAreaElement).style.height).toBe("96px");
    expect((textarea.element as HTMLTextAreaElement).style.overflowY).toBe("hidden");
    Object.defineProperty(textarea.element, "scrollHeight", { configurable: true, value: 320 });
    await textarea.setValue("更多内容\n".repeat(20));
    expect((textarea.element as HTMLTextAreaElement).style.height).toBe("192px");
    expect((textarea.element as HTMLTextAreaElement).style.overflowY).toBe("auto");
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
