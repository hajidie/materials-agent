import { afterEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import App from "../../src/App.vue";
import SegmentationBatch from "../../src/components/SegmentationBatch.vue";
import { useAgentRuns } from "../../src/composables/useAgentRuns";
import type { AgentRun, ChatMessage } from "../../src/api/agent";

afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

it("reconciles the original conversation before uploading more files after a lost create response", async () => {
  const keys: string[] = [];
  let lost = true;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/conversations") && init?.method === "POST") {
      keys.push(new Headers(init.headers).get("Idempotency-Key")!);
      if (lost) throw new TypeError("lost create response");
      return new Response(JSON.stringify({request_id:"req",data:{conversation_id:"recovered",title:null,
        created_at:"2026-10-09T00:00:00Z",updated_at:"2026-10-09T00:00:00Z"}}));
    }
    return new Response(JSON.stringify({data:{items:[],next_cursor:null}}));
  }));
  let state!: ReturnType<typeof useAgentRuns>;
  const wrapper = mount({setup() { state = useAgentRuns(); return () => null; }});
  await flushPromises();
  const files = ["one.png", "two.png"].map(name => new File(["image"], name, {type:"image/png"}));
  await state.uploadAttachments(files);
  expect(keys).toHaveLength(1);
  await state.uploadAttachments(files);
  expect(keys).toHaveLength(1);
  lost = false;
  await state.checkUpload();
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  expect(state.uploadEntries.value.map(value => ({conversationId:value.conversationId,context:value.context,message:value.message}))).toEqual([
    expect.objectContaining({conversationId:"recovered"}), expect.objectContaining({conversationId:"recovered"}),
  ]);
  wrapper.unmount();
});

it("uploads each image with its own identity, continues after failure and submits confirmed images only", async () => {
  sessionStorage.clear(); sessionStorage.setItem("materials-agent.selected-conversation.v1", "conv");
  const keys: string[] = [];
  const messages: Record<string, unknown>[] = [];
  const response = (data: unknown, status = 200) => new Response(JSON.stringify({data}), {status});
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/attachments")) {
      keys.push(new Headers(init?.headers).get("Idempotency-Key")!);
      const file = (init?.body as FormData).get("file") as File;
      if (file.name === "bad.png") return response({error: "invalid"}, 422);
      return response({attachment: {attachment_id: `asset_${file.name}`, name: file.name, kind: "image"}});
    }
    if (url.endsWith("/messages") && init?.method === "POST") {
      messages.push(JSON.parse(String(init.body))); throw new TypeError("lost submission");
    }
    if (url.includes("/assets/")) return response({status:"AVAILABLE", width:128,height:160,media_type:"image/png"});
    if (url.endsWith("/ui-state")) return response({fence:{operation_id:null}});
    if (url.endsWith("/results/reconcile")) return response({messages:[],pending:false});
    return response({items: url.endsWith("/conversations") ? [{conversation_id:"conv",title:"TC4"}] : [],next_cursor:null});
  }));
  const wrapper = mount(App); await flushPromises();
  const input = wrapper.get('input[type="file"]');
  Object.defineProperty(input.element, "files", {value: ["one.png", "bad.png", "two.png"].map(name => new File(["image"], name, {type:"image/png"}))});
  await input.trigger("change"); await flushPromises();
  expect(keys).toHaveLength(3); expect(new Set(keys).size).toBe(3);
  expect(wrapper.findAll('.composer__attachment img')).toHaveLength(2);
  expect(wrapper.text()).toContain("bad.png");
  await wrapper.get("textarea").setValue("分割 TC4 初生 α 相");
  await wrapper.get("form").trigger("submit"); await flushPromises();
  expect(messages[0]!.attachments).toEqual([
    {attachment_id:"asset_one.png",name:"one.png",kind:"image"},
    {attachment_id:"asset_two.png",name:"two.png",kind:"image"},
  ]);
  wrapper.unmount();
});

it("groups both PNG results and formats a stored fraction as a percentage", () => {
  const wrapper = mount(SegmentationBatch, {props:{message:"message",items:[
    {ordinal:0,name:"sample.png",status:"SUCCEEDED",width:604,height:604,foreground_pixels:176054,
      total_pixels:364816,area_fraction:176054/364816,artifacts:{overlay:"overlay",mask:"mask"},error:null},
    {ordinal:1,name:"failed.png",status:"FAILED",artifacts:{},error:{code:"INPUT_UNAVAILABLE",message:"输入图片不可用。"}},
  ]},global:{stubs:{EbsdImage:true}}});
  expect(wrapper.text()).toContain("48.26%");
  expect(wrapper.findAll(".segmentation-batch__image").filter(button => button.isVisible())).toHaveLength(2);
  expect(wrapper.text()).toContain("输入图片不可用。");
  wrapper.unmount();
});

it("restores grouped results beyond the latest 20 runs when loading older messages after reopening", async () => {
  sessionStorage.setItem("materials-agent.selected-conversation.v1", "conv");
  const timestamp = "2026-10-09T00:00:00Z";
  const run = (index: number): AgentRun => ({
    agent_run_id: `run-${index}`, conversation_id: "conv", source_message_id: `user-${index}`,
    source_answer_message_id: null, answer_root_message_id: null, goal: "TC4", status: "SUCCEEDED", version: 1,
    waiting_version: 0, waiting: null, submission_id: null, question_message_id: null, final_message_id: `answer-${index}`,
    stopped: false, pending_execution: null, executions: [], observations: [], error_message: null, outcome_unknown: false,
    attachments: [], result_attachments: [], created_at: timestamp,
  });
  const latest = Array.from({length: 20}, (_, index) => run(index + 2));
  const historical: AgentRun = {...run(1), segmentation_items: [{ordinal: 0, name: "historical.png", status: "SUCCEEDED",
    width: 604, height: 604, foreground_pixels: 176054, total_pixels: 364816, area_fraction: 176054 / 364816,
    artifacts: {overlay: "historical-overlay", mask: "historical-mask"}, error: null}]};
  const message = (index: number): ChatMessage => ({
    message_id: `answer-${index}`, agent_run_id: `run-${index}`, role: "ASSISTANT", phase: "answer",
    content_status: "complete", text: `第 ${index} 次分割回答`, sequence: index * 2, created_at: timestamp,
    attachments: [], artifacts: [], answer_root_message_id: `answer-${index}`, answer_version: 1, version_count: 1,
  });
  const response = (data: unknown) => new Response(JSON.stringify({request_id: "req", data}));
  const historicalReads: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/results/reconcile")) return response({messages: [], pending: false});
    expect(init?.method ?? "GET").toBe("GET");
    if (url.endsWith("/conversations/conv/agent-runs")) return response({items: latest, next_cursor: "run-2"});
    if (url.endsWith("/conversations/conv/messages?before=4")) return response({items: [message(1)], next_cursor: null});
    if (url.endsWith("/conversations/conv/messages")) return response({items: latest.map((_, index) => message(index + 2)), next_cursor: 4});
    if (url.endsWith("/agent-runs/run-1")) { historicalReads.push(url); return response(historical); }
    if (url.endsWith("/ui-state")) return response({fence: {operation_id: null}});
    return response({items: [{conversation_id: "conv", title: "TC4", created_at: timestamp, updated_at: timestamp, last_activity_preview: null}], next_cursor: null});
  }));
  for (let visit = 0; visit < 2; visit++) {
    const wrapper = mount(App, {global: {stubs: {ResearchProcess: true, EbsdImage: true, AgentRunCard: true}}});
    try {
      await flushPromises();
      expect(wrapper.text()).not.toContain("historical.png");
      await wrapper.findAll("button").find(button => button.text() === "加载更早的消息")!.trigger("click");
      await flushPromises();
      expect(wrapper.text()).toContain("第 1 次分割回答");
      expect(wrapper.get(".segmentation-batch").text()).toContain("historical.png");
      expect(wrapper.get(".segmentation-batch").text()).toContain("48.26%");
      expect(wrapper.get(".segmentation-batch").text()).toContain("176054 / 364816");
      expect(historicalReads).toHaveLength(visit + 1);
    } finally { wrapper.unmount(); }
  }
});

it("discards a late historical run response after changing conversations", async () => {
  sessionStorage.setItem("materials-agent.selected-conversation.v1", "original");
  let release!: (response: Response) => void;
  const historicalResponse = new Promise<Response>(resolve => { release = resolve; });
  const response = (data: unknown) => new Response(JSON.stringify({data}));
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/agent-runs/historical")) return historicalResponse;
    if (url.endsWith("/conversations/original/messages")) return response({items: [{
      message_id: "old-answer", agent_run_id: "historical", role: "ASSISTANT", phase: "answer", sequence: 1,
    }], next_cursor: null});
    return response({items: [], next_cursor: null});
  }));
  let state!: ReturnType<typeof useAgentRuns>;
  const wrapper = mount({setup() { state = useAgentRuns(); return () => null; }});
  try {
    const originalRefresh = state.refresh();
    await flushPromises();
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith("/agent-runs/historical"))).toBe(true);
    await state.select("other");
    release(response({agent_run_id: "historical", conversation_id: "original", version: 1, status: "SUCCEEDED"}));
    await originalRefresh;
    expect(state.selectedId.value).toBe("other");
    expect(state.messages.value).toEqual([]);
    expect(state.runs.value).toEqual([]);
  } finally { wrapper.unmount(); }
});
