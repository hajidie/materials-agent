import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../../src/App.vue";
import type { AgentRun } from "../../src/api/agent";
const timestamp = "2026-09-11T00:00:00Z";
const conversation = { conversation_id: "conv", title: "研究", created_at: timestamp, updated_at: timestamp };
function run(patch: Partial<AgentRun> = {}): AgentRun {
 return {agent_run_id: "run", conversation_id: "conv", source_message_id: "msg", goal: "材料研究", status: "SUCCEEDED", version: 8, waiting_version: 0, waiting: null, pending_execution: null, executions: [], observations: [], final_answer: {text: "已完成研究", answer_id: "answer"}, error_message: null, outcome_unknown: false, attachments: [], result_attachments: [], user_inputs: [], user_messages: [{message_id: "msg", text: "材料研究", attachments: []}], created_at: timestamp, ...patch};
}
const response = (data: unknown) => new Response(JSON.stringify({request_id: "req", data}), {status: 200});
let items: AgentRun[]; let fail: boolean;
let writes: Array<{url: string; body: Record<string, unknown>; key: string}>;
let writeGate: Promise<void> | null;
const wrappers: Array<ReturnType<typeof mount>> = [];
async function show() {const wrapper = mount(App); wrappers.push(wrapper); await flushPromises(); return wrapper;}
function button(wrapper: ReturnType<typeof mount>, text: string) {const found = wrapper.findAll("button").find(b => b.text() === text); if (!found) throw new Error(text); return found;}
beforeEach(() => {
 sessionStorage.clear(); sessionStorage.setItem("materials-agent.selected-conversation.v1", "conv"); items = []; writes = []; fail = false; writeGate = null;
 vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  if (url.endsWith("/results/reconcile")) return response({messages: [], pending: false});
  if (url.endsWith("/result-messages")) return response({items: []});
  if (url.endsWith("/ui-state")) return response({fence: {operation_id: null}});
  if (init?.method === "POST") {writes.push({url, body: JSON.parse(String(init.body)), key: new Headers(init.headers).get("Idempotency-Key") ?? ""});
   if (url.endsWith("/conversations")) return response(conversation); if (writeGate) await writeGate;
   if (fail) throw new TypeError("network"); items = [run()]; return response({agent_run: items[0]}); }
  if (url.includes("/agent-runs")) return response({items, next_cursor: null});
  return response({items: [conversation], next_cursor: null});
 }));
});
afterEach(() => {wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals();});
it("switches from a centered empty composer to the conversation layout", async () => {
 const w = await show();
 expect(w.get("main").classes()).toContain("app-main--empty");
 expect(w.text()).not.toContain("刷新运行状态");
 expect(w.text()).not.toContain("刷新对话列表");
 expect(w.text()).not.toContain("描述你的研究目标");
 expect(w.find(".conversation-header").exists()).toBe(false);
 expect(w.get(".conversation-scroll").find(".composer").exists()).toBe(false);
 expect(w.get(".composer").element.parentElement).toBe(w.get("main").element);
 expect(w.find('[aria-label="对话消息"]').exists()).toBe(false);
 await w.get("textarea").setValue("1000 MPa 转 GPa");
 await w.get("form").trigger("submit"); await flushPromises();
 expect(w.get("main").classes()).not.toContain("app-main--empty");
 expect(w.find('[aria-label="对话消息"]').exists()).toBe(true);
 await button(w, "新建对话").trigger("click"); await flushPromises();
 expect(w.get("main").classes()).toContain("app-main--empty");
});
it("reads persisted answers without writes", async () => {items = [run()]; const w = await show(); expect(w.text()).toContain("已完成研究"); expect(writes).toHaveLength(0);});
it("submits a goal and clears its acknowledged draft", async () => {
 const w = await show(); await w.get("textarea").setValue("1000 MPa 转 GPa"); await w.get("form").trigger("submit"); await flushPromises();
 expect(writes[0]?.body).toEqual({mode: "NEW_RUN", content_text: "1000 MPa 转 GPa", attachments: []}); expect((w.get("textarea").element as HTMLTextAreaElement).value).toBe("");
});
it("hides the global pending banner and sent draft while a submission is running", async () => {
 let release = () => {}; writeGate = new Promise<void>(resolve => { release = resolve; });
 const w = await show(); await w.get("textarea").setValue("研究目标"); await w.get("form").trigger("submit"); await flushPromises();
 expect(w.text()).not.toContain("请求正在执行，进度会自动更新。");
 expect(w.text()).not.toContain("检查原提交");
 expect((w.get("textarea").element as HTMLTextAreaElement).value).toBe("");
 release(); await flushPromises();
});
it("resumes the exact Run and waiting version", async () => {
 items = [run({status: "WAITING_FOR_USER", final_answer: null, waiting_version: 3, waiting: {reason: "TOOL_ARGUMENT_CLARIFICATION", question: "请提供温度"}})];
 const w = await show(); await button(w, "补充信息").trigger("click"); await w.get("textarea").setValue("1000 摄氏度"); await w.get("form").trigger("submit"); await flushPromises();
 expect(writes[0]?.body).toEqual({mode: "RESUME_RUN", content_text: "1000 摄氏度", agent_run_id: "run", waiting_version: 3, attachments: []});
});
it("preserves its key across network uncertainty and reload", async () => {
 fail = true; const w = await show(); await w.get("textarea").setValue("研究目标"); await w.get("form").trigger("submit"); await flushPromises();
 expect(w.text()).toContain("结果尚不确定"); const key = writes[0]?.key; expect(key).toBeTruthy(); expect(w.get("textarea").attributes()).toHaveProperty("disabled");
 w.unmount(); wrappers.pop(); fail = false; const reloaded = await show(); await button(reloaded, "检查原提交").trigger("click"); await flushPromises();
 expect(writes[1]?.key).toBe(key); expect(writes[1]?.body).toEqual(writes[0]?.body); expect(sessionStorage.getItem("materials-agent.pending-run.v2")).toBeNull(); expect((reloaded.get("textarea").element as HTMLTextAreaElement).value).toBe("");
});
it("binds confirmation to Invocation and parameter version", async () => {
 items = [run({status: "WAITING_FOR_CONFIRMATION", final_answer: null, waiting_version: 2, pending_execution: {invocation_run_id: "inv", tool_name: "test", status: "PENDING_CONFIRMATION", confirmation: [{label: "数值", value: 2}], confirmation_version: "hash", confirmation_expires_at: null, retryable: false}})];
 const w = await show(); await button(w, "确认执行").trigger("click"); await flushPromises(); expect(writes[0]?.url).toContain("/run/invocations/inv/confirm"); expect(writes[0]?.body).toEqual({waiting_version: 2, confirmation_version: "hash"});
});
it("renders model markup as text", async () => {items = [run({final_answer: {text: '<img src=x onerror="alert(1)">', answer_id: "answer"}})]; const w = await show(); expect(w.findAll("img")).toHaveLength(0); expect(w.text()).toContain("onerror");});
