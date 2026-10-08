import { mount } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import AgentRunCard from "../../src/components/AgentRunCard.vue";
import type { AgentRun } from "../../src/api/agent";

const base: AgentRun = {
  agent_run_id: "run", conversation_id: "conversation", source_message_id: "message", source_answer_message_id: null,
  answer_root_message_id: null, goal: "研究", status: "INTERRUPTED", version: 3, waiting_version: 0,
  waiting: null, submission_id: "submission", question_message_id: null, final_message_id: null, stopped: false,
  pending_execution: null, executions: [], observations: [], error_message: "模型暂时不可用", outcome_unknown: false,
  attachments: [], result_attachments: [], created_at: "2026-10-08T00:00:00Z", can_resume: true, recovery_action: "CONTINUE",
};
const mounted: ReturnType<typeof mount>[] = [];
function card(patch: Partial<AgentRun> = {}) {
  const value = mount(AgentRunCard, { props: { run: { ...base, ...patch }, disabled: false },
    global: { stubs: { ResearchProcess: true } } });
  mounted.push(value);
  return value;
}
afterEach(() => { mounted.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers(); });

it("offers continuing and ending after transient exhaustion", async () => {
  const wrapper = card();
  await wrapper.get("button.button--primary").trigger("click");
  expect(wrapper.emitted("resume")).toHaveLength(1);
  expect(wrapper.text()).toContain("结束本次处理");
});

it("keeps unknown external work pending and offers reconciliation", async () => {
  const wrapper = card({ can_resume: false, recovery_action: "RECONCILE", outcome_unknown: true,
    pending_execution: { invocation_run_id: "private-invocation", tool_name: "ebsd_yield_strength_predictor", status: "OUTCOME_UNKNOWN",
      confirmation: [], confirmation_version: null, confirmation_expires_at: null, retryable: false } });
  expect(wrapper.text()).not.toContain("继续处理");
  expect(wrapper.text()).not.toContain("private-invocation");
  await wrapper.findAll("button").find(button => button.text() === "核查原操作")!.trigger("click");
  expect(wrapper.emitted("reconcile")).toEqual([["private-invocation"]]);
});

it("enables continuing only after the server Retry-After time", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-08T00:00:00Z"));
  const wrapper = card({ can_resume: false, resume_after: "2026-10-08T00:00:02Z" });
  expect(wrapper.get("button.button--primary").attributes("disabled")).toBeDefined();
  await vi.advanceTimersByTimeAsync(2100);
  expect(wrapper.get("button.button--primary").attributes("disabled")).toBeUndefined();
  expect(wrapper.get("button.button--primary").text()).toBe("继续处理");
});

it("explains configuration repair and hides continuing when the budget is exhausted", () => {
  expect(card({ recovery_action: "FIX_CONFIGURATION" }).text()).toContain("修正后继续处理");
  const exhausted = card({ recovery_action: "NONE", can_resume: false });
  expect(exhausted.text()).not.toContain("继续处理");
  expect(exhausted.text()).toContain("结束本次处理");
});
