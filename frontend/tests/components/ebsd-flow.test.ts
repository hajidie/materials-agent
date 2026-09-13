import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import App from "../../src/App.vue";
import ResearchResultSummary from "../../src/components/ResearchResultSummary.vue";

const conversation = { conversation_id: "conv", title: "EBSD", created_at: "2026-09-13T00:00:00Z", updated_at: "2026-09-13T00:00:00Z", last_activity_preview: null };
const response = (data: unknown, status = 200) => new Response(JSON.stringify({ request_id: "req", data }), { status });
let failUpload: boolean;
let uploads: string[];
let messages: Record<string, unknown>[];
const wrappers: ReturnType<typeof mount>[] = [];
beforeEach(() => {
  sessionStorage.clear(); sessionStorage.setItem("materials-agent.selected-conversation.v1", "conv");
  failUpload = false; uploads = []; messages = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/ebsd-images")) {
      uploads.push(new Headers(init?.headers).get("Idempotency-Key") ?? "");
      if (failUpload) throw new TypeError("network");
      return response({ asset_id: "asset_image" });
    }
    if (url.endsWith("/messages")) { messages.push(JSON.parse(String(init?.body))); throw new TypeError("network"); }
    if (url.includes("/assets/")) return response({ asset_id: "asset_image", status: "AVAILABLE", width: 200, height: 200, media_type: "image/png", role: "supporting" });
    if (url.includes("/agent-runs")) return response({ items: [], next_cursor: null });
    return response({ items: [conversation], next_cursor: null });
  }));
});
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals(); });
async function show() { const wrapper = mount(App); wrappers.push(wrapper); await flushPromises(); return wrapper; }
async function selectImage(wrapper: ReturnType<typeof mount>) {
  const input = wrapper.get('input[type="file"]');
  Object.defineProperty(input.element, "files", { value: [new File(["test"], "ebsd.png", { type: "image/png" })], configurable: true });
  await input.trigger("change"); await flushPromises();
}

it("submits an uploaded EBSD reference and preserves it across uncertain reload", async () => {
  const wrapper = await show(); await selectImage(wrapper);
  await wrapper.get("textarea").setValue("预测屈服强度");
  await wrapper.get("form").trigger("submit"); await flushPromises();
  expect(messages[0]).toMatchObject({ mode: "NEW_RUN", ebsd_asset_id: "asset_image", content_text: "预测屈服强度" });
  wrapper.unmount(); wrappers.pop();
  const reloaded = await show();
  expect(reloaded.text()).toContain("检查原提交");
  expect((reloaded.get("textarea").element as HTMLTextAreaElement).value).toBe("预测屈服强度");
  expect(reloaded.findComponent({ name: "EbsdImage" }).exists()).toBe(true);
  expect(uploads).toHaveLength(1);
});

it("retries an uncertain upload with the same key without submitting a message", async () => {
  failUpload = true;
  const wrapper = await show(); await selectImage(wrapper);
  expect(messages).toHaveLength(0);
  failUpload = false;
  await wrapper.findAll("button").find(b => b.text() === "重试上传")!.trigger("click");
  await flushPromises();
  expect(uploads).toHaveLength(2); expect(uploads[0]).toBe(uploads[1]);
  expect(wrapper.text()).toContain("移除 EBSD 图片");
});

it("renders a single generic performance metric without SEM process fields", () => {
  const wrapper = mount(ResearchResultSummary, { props: { result: {
    result_id: "result", tool_run_id: "run", tool_id: "ebsd_yield_strength_predictor", tool_version: "1", schema_hash: "hash",
    status: "SUCCEEDED", requested_outputs: ["yield_strength"], completed_outputs: ["yield_strength"], failed_outputs: [],
    data: { yield_strength: { value: 404.905151, unit: "MPa" } }, provenance: { material: "Inconel 625", model_version: "inconel625-cnn1-v1" },
    warnings: [], error: null, created_at: "2026-09-13T00:00:00Z",
  } } }); wrappers.push(wrapper);
  expect(wrapper.text()).toContain("404.91 MPa");
  expect(wrapper.text()).not.toContain("延伸率");
  expect(wrapper.text()).not.toContain("实验条件暂不可用");
  expect(wrapper.text()).toContain("尚未核实");
});


it("keeps an uncertain resume asset on reload without copying it into a new target draft", async () => {
  sessionStorage.setItem("materials-agent.pending-run.v1", JSON.stringify({
    path: "/conversations/conv/messages", conversationId: "conv", key: "original-resume",
    body: {mode: "RESUME_RUN", content_text: "补图", agent_run_id: "waiting", waiting_version: 2, ebsd_asset_id: "asset_image"},
  }));
  const wrapper = await show();
  expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe("补图");
  expect(wrapper.findComponent({name:"EbsdImage"}).exists()).toBe(true);
  expect(sessionStorage.getItem("materials-agent.ebsd-drafts.v1") ?? "{}").not.toContain("补图");
});

it("does not show another conversation's uncertain text or image", async () => {
  sessionStorage.setItem("materials-agent.pending-run.v1", JSON.stringify({
    path: "/conversations/other/messages", conversationId: "other", key: "original-other",
    body: {mode: "NEW_RUN", content_text: "other draft", ebsd_asset_id: "asset_other"},
  }));
  const wrapper = await show();
  expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe("");
  expect(wrapper.findComponent({name:"EbsdImage"}).exists()).toBe(false);
});
