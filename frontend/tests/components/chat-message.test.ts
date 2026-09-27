import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import ChatMessage from "../../src/components/ChatMessage.vue";
import type { ChatMessage as Message } from "../../src/api/agent";

const answer: Message = { message_id: "v2", agent_run_id: "run2", role: "ASSISTANT", phase: "answer",
  content_status: "complete", text: "第二版", sequence: 2, created_at: "2026-09-27T00:00:00Z",
  attachments: [], artifacts: [], answer_root_message_id: "v1", answer_version: 2, version_count: 2 };
afterEach(() => vi.unstubAllGlobals());

it("copies and regenerates the displayed version at the same message position", async () => {
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText: copy }, configurable: true });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: {
    items: [{ ...answer, message_id: "v1", text: "第一版", answer_version: 1 }] } }), { status: 200 })));
  const w = mount(ChatMessage, { props: { message: answer, conversationId: "conv", disabled: false } });
  await w.get('[aria-label="上一个回答版本"]').trigger("click"); await flushPromises();
  expect(w.text()).toContain("第一版"); expect(w.text()).toContain("1 / 2");
  await w.get('[aria-label="复制回答"]').trigger("click"); await flushPromises();
  expect(copy).toHaveBeenCalledWith("第一版");
  await w.get('[aria-label="重新生成回答"]').trigger("click");
  expect((w.emitted("regenerate")![0]![0] as Message).message_id).toBe("v1");
  w.unmount();
});

it("questions allow copy but have no regenerate action", () => {
  const w = mount(ChatMessage, { props: { message: { ...answer, phase: "question", answer_version: null,
    answer_root_message_id: null, version_count: 0 }, conversationId: "conv", disabled: false } });
  expect(w.find('[aria-label="复制回答"]').exists()).toBe(true);
  expect(w.find('[aria-label="重新生成回答"]').exists()).toBe(false);
  w.unmount();
});

it("does not install a stale fetched version after a new answer arrives", async () => {
  let resolve!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(r => { resolve = r; })));
  const w = mount(ChatMessage, { props: { message: answer, conversationId: "conv", disabled: false } });
  await w.get('[aria-label="上一个回答版本"]').trigger("click");
  await w.setProps({ message: { ...answer, message_id: "v3", text: "第三版", answer_version: 3, version_count: 3 } });
  resolve(new Response(JSON.stringify({ data: { items: [{ ...answer, message_id: "v1", text: "第一版", answer_version: 1 }] } })));
  await flushPromises(); expect(w.text()).toContain("第三版"); expect(w.text()).not.toContain("第一版");
  w.unmount();
});
