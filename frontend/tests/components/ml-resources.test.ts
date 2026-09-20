import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../../src/App.vue";
import ResultPresentation from "../../src/components/ResultPresentation.vue";
import type { AgentRun } from "../../src/api/agent";
import type { ResultMessage } from "../../src/api/artifacts";

const timestamp = "2026-09-15T00:00:00Z";
const attachment = { attachment_id: "private-ref", kind: "dataset" as const, name: "Inconel 实验.csv" };
const presentation = { title: "数据概况", summary: "数据可用于分析。", facts: { row_count: 50 }, metrics: [], notes: [] };
const response = (data: unknown) => new Response(JSON.stringify({ request_id: "request", data }), { status: 200 });
const calls: { url: string; method: string; body?: BodyInit | null }[] = [];
const wrappers: ReturnType<typeof mount>[] = [];
let fence: string | null, run: AgentRun, results: ResultMessage[];
function button(w: ReturnType<typeof mount>, text: string) { const value = w.findAll("button").find(b => b.text() === text); if (!value) throw new Error(text); return value; }
async function show() { const w = mount(App, { attachTo: document.body }); wrappers.push(w); await flushPromises(); return w; }
beforeEach(() => {
  sessionStorage.clear(); sessionStorage.setItem("materials-agent.selected-conversation.v1", "conv");
  calls.length = 0; fence = null; results = [];
  run = { agent_run_id: "private-run", conversation_id: "conv", source_message_id: "private-message", goal: "分析实验数据", status: "SUCCEEDED", version: 1,
    waiting_version: 0, waiting: null, pending_execution: null, executions: [], observations: [], final_answer: { answer_id: "answer", text: "训练已提交。" },
    error_message: null, outcome_unknown: false, user_inputs: [], user_messages: [{message_id: "private-message", text: "分析实验数据", attachments: [attachment]}], attachments: [attachment], result_attachments: [], created_at: timestamp };
  HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) { this.setAttribute("open", ""); });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), method = init?.method ?? "GET";
    calls.push({ url, method, ...(init?.body !== undefined ? { body: init.body } : {}) });
    if (url.endsWith("/ui-state")) return response({ fence: { operation_id: fence } });
    if (url.endsWith("/result-messages")) return response({ items: results });
    if (url.endsWith("/results/reconcile")) return response({ messages: results, pending: false });
    if (url.includes("/artifacts/")) return response({ ...attachment, presentation, downloads: [{ name: "下载 CSV", url: "/controlled.csv" }] });
    if (url.endsWith("/attachments")) return response({ attachment });
    if (url.endsWith("/agent-runs")) return response({ items: [run], next_cursor: null });
    if (url === "/api/v1/conversations?limit=20") return response({ items: [{ conversation_id: "conv", title: "研究", created_at: timestamp, updated_at: timestamp }], next_cursor: null });
    return response({ items: [], next_cursor: null });
  }));
});
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.useRealTimers(); vi.unstubAllGlobals(); document.body.innerHTML = ""; });

it("shows inferred unit provenance without replacing undeclared metadata", () => {
  const w = mount(ResultPresentation, { props: { details: true, value: {
    ...presentation, facts: { units: { strength_MPa: null } },
    metrics: [{ label: "MAE", value: 7.18, unit: null }],
    notes: ["变量 strength_MPa 根据字段名推断为 MPa（模型语义推断，尚未确认；未用于数值换算）。"],
    unit_annotations: [{ resource_parameter: "dataset_reference", column: "strength_MPa", unit: "MPa", inferred_unit: "MPa",
      source: "model_inference", evidence: "strength_MPa", usage: "interpretation", provenance: "inferred", conflict: false, requires_confirmation: false }]
  } } });
  wrappers.push(w);
  expect(w.text()).toContain("未声明"); expect(w.text()).toContain("模型语义推断");
  expect(w.get(".artifact-metrics").text()).not.toContain("MPa");
  expect(w.text()).not.toContain("model_inference");
});

it("views a message attachment with GET only and preserves composer and selection state", async () => {
  const w = await show(); await w.get("textarea").setValue("保持草稿");
  const before = JSON.stringify(run), writes = calls.filter(c => c.method === "POST").length;
  const trigger = w.get(".attachment-card"); (trigger.element as HTMLButtonElement).focus(); await trigger.trigger("click"); await flushPromises();
  expect(w.get("dialog").text()).toContain("样本数量"); expect(w.get("dialog").text()).toContain("50");
  expect(calls.filter(c => c.method === "POST")).toHaveLength(writes);
  expect(calls.some(c => /select|discover-model|resources\?/.test(c.url))).toBe(false);
  await button(w, "关闭").trigger("click"); await flushPromises();
  expect((w.get("textarea").element as HTMLTextAreaElement).value).toBe("保持草稿"); expect(JSON.stringify(run)).toBe(before);
  expect(document.activeElement).toBe(trigger.element);
});

it("has one upload entry and no resource management or internal identity text", async () => {
  const w = await show();
  expect(w.findAll('input[type="file"]')).toHaveLength(1);
  expect(w.get('input[type="file"]').attributes("accept")).toContain(".csv");
  for (const value of ["资源中心", "数据集列表", "TrainingRun", "private-ref", "private-run", "SUCCEEDED"]) expect(w.text()).not.toContain(value);
  const input = w.get('input[type="file"]');
  Object.defineProperty(input.element, "files", { value: [new File(["x,y\n1,2"], "Inconel 实验.csv", { type: "text/csv" })] });
  await input.trigger("change"); await flushPromises();
  const upload = calls.find(c => c.url.endsWith("/attachments"));
  expect(upload?.body).toBeInstanceOf(FormData); expect(w.get(".attachment-name").text()).toContain("Inconel");
});

it("keeps initial replies immutable and displays a terminal result once across reload", async () => {
  results = [{ message_id: "terminal-message", text: "模型训练完成。R² = 0.957", created_at: "2026-09-15T00:01:00Z", presentation, artifacts: [attachment] }];
  let w = await show(); expect(w.text()).toContain("训练已提交。"); expect(w.findAll(".chat-result")).toHaveLength(1);
  expect(run.final_answer?.text).toBe("训练已提交。");
  w.unmount(); wrappers.pop(); w = await show(); expect(w.findAll(".chat-result")).toHaveLength(1);
  expect(w.text()).toContain("0.957");
});

it("keeps deletion fences after refresh and never starts observation under a known fence", async () => {
  fence = "deletion-private"; const w = await show();
  expect(w.get("textarea").attributes()).toHaveProperty("disabled");
  expect(w.text()).toContain("新工作已暂停"); expect(w.text()).not.toContain("deletion-private");
  expect(calls.filter(c => c.url.endsWith("/results/reconcile"))).toHaveLength(0);
});

it("unknown execution only offers original receipt checks and hides diagnostic codes", async () => {
  run.status = "TERMINATED"; run.outcome_unknown = true; run.final_answer = null;
  run.executions = [{ invocation_run_id: "internal-invocation", tool_name: "materials_ml_train_tabular_regression", status: "OUTCOME_UNKNOWN", retryable: false, confirmation: [], confirmation_version: null, confirmation_expires_at: null }];
  const w = await show(); expect(button(w, "核查原操作").exists()).toBe(true);
  expect(w.text()).not.toContain("OUTCOME_UNKNOWN"); expect(w.text()).not.toContain("internal-invocation");
  expect(w.text()).not.toContain("重新生成回答");
});

it("discards old drafts instead of replaying an incompatible message", async () => {
  sessionStorage.setItem("materials-agent.pending-run.v1", JSON.stringify({ body: { ebsd_asset_id: "old" } }));
  sessionStorage.setItem("materials-agent.ebsd-drafts.v1", JSON.stringify({ "conv:new": { text: "old", assetId: "old" } }));
  const w = await show(); expect((w.get("textarea").element as HTMLTextAreaElement).value).toBe("");
  expect(sessionStorage.getItem("materials-agent.pending-run.v1")).toBeNull();
  expect(calls.some(c => String(c.body).includes("ebsd_asset_id"))).toBe(false);
});
