import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ToolTaskCard from "../../src/components/ToolTaskCard.vue";
import type {
  AssetSummary,
  ExplanationSummary,
  ResultSummary,
  TaskStatus,
  TimelineToolTaskItem,
  ToolRunSummary,
} from "../../src/api/types";

const timestamp = "2026-08-04T00:00:00Z";
const rawStatusPattern =
  /SUCCEEDED|PARTIALLY_SUCCEEDED|FAILED|PENDING|RUNNING|NEEDS_INPUT/;

function toolRun(
  status: ToolRunSummary["status"] = "SUCCEEDED",
): ToolRunSummary {
  return {
    tool_run_id: "private-tool-run-id",
    attempt_no: 2,
    tool_id: "zta35g_sem_virtual_lab",
    tool_version: "private-tool-version",
    schema_hash: "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07",
    status,
    requested_outputs: ["sem_image", "mechanical_properties"],
    completed_outputs: status === "SUCCEEDED" ? ["sem_image", "mechanical_properties"] : [],
    failed_outputs: status === "SUCCEEDED" ? [] : ["sem_image", "mechanical_properties"],
    created_at: timestamp,
    started_at: timestamp,
    completed_at: timestamp,
    duration_ms: 1234,
    diagnostics_summary: [
      {
        step: "private-diagnostic-step",
        status: "FAILED",
        duration_ms: 999,
        error_code: "PRIVATE_DIAGNOSTIC_CODE",
        safe_error_message: "不应显示的 ToolRun 诊断。",
      },
    ],
    error: {
      code: "PRIVATE_TOOL_RUN_CODE",
      message: "不应显示的 ToolRun 错误。",
    },
    is_selected: true,
  };
}

function result(overrides: Partial<ResultSummary> = {}): ResultSummary {
  return {
    result_id: "result-1",
    tool_run_id: "private-tool-run-id",
    status: "SUCCEEDED",
    requested_outputs: ["sem_image", "mechanical_properties"],
    completed_outputs: ["sem_image", "mechanical_properties"],
    failed_outputs: [],
    data: {
      elongation: { value: 3.456, unit: "%" },
      yield_strength: { value: 650.1, unit: "MPa" },
      raw_json: { private_payload: "不应显示的原始结果" },
    },
    warnings: [],
    provenance: {
      normalized_process_parameters: {
        solution_temperature: { value: 1020, unit: "°C" },
        solution_time: { value: 1.5, unit: "h" },
        aging_temperature: { value: 480, unit: "°C" },
        aging_time: { value: 8, unit: "h" },
      },
      private_payload: "不应显示的来源数据",
    },
    error: null,
    tool_id: "zta35g_sem_virtual_lab",
    tool_version: "private-tool-version",
    schema_hash: "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07",
    created_at: timestamp,
    ...overrides,
  };
}

function asset(): AssetSummary {
  return {
    asset_id: "asset-1",
    status: "AVAILABLE",
    role: "sem_image",
    media_type: "image/png",
    width: 512,
    height: 512,
    bit_depth: 8,
    size_bytes: 262144,
    sha256: "a".repeat(64),
    content_url: "/api/v1/assets/asset-1/content",
  };
}

function explanation(
  status: ExplanationSummary["status"] = "SUCCEEDED",
  attemptNo = 1,
): ExplanationSummary {
  return {
    explanation_id: `explanation-${attemptNo}`,
    attempt_no: attemptNo,
    status,
    language: "zh-CN",
    text: status === "SUCCEEDED" ? "这是已持久化的成功说明。" : null,
    completed_at: timestamp,
    duration_ms: 80,
    error_code: status === "FAILED" ? "PRIVATE_EXPLANATION_CODE" : null,
    safe_error_message: status === "FAILED" ? "说明服务暂时不可用。" : null,
  };
}

function item(taskStatus: TaskStatus = "SUCCEEDED"): TimelineToolTaskItem {
  return {
    item_type: "TOOL_TASK",
    item_id: "task-1",
    task_id: "task-1",
    anchor_at: timestamp,
    initial_user_message: {
      message_id: "message-initial",
      role: "USER",
      content_text: "生成组织图像并预测性能",
      created_at: timestamp,
    },
    task: {
      task_id: "task-1",
      task_type: "TOOL_EXECUTION",
      status: taskStatus,
      selected_tool_run_id: "private-tool-run-id",
      selected_result_id: "result-1",
      created_at: timestamp,
      started_at: timestamp,
      updated_at: timestamp,
      completed_at: timestamp,
      error_code: null,
      safe_error_message: null,
      tool_id: "zta35g_sem_virtual_lab",
      bound_tool_version: "1",
      bound_schema_hash: "a".repeat(64),
    },
    input_thread: [
      {
        message_id: "message-follow-up",
        role: "ASSISTANT",
        content_text: "请补充时效时间",
        created_at: timestamp,
      },
    ],
    input_thread_count: 1,
    input_thread_truncated: false,
    tool_runs: {
      attempt_count: 2,
      selected_tool_run: toolRun(),
      has_history: true,
    },
    result: result(),
    assets: [asset()],
    explanation: explanation(),
    latest_explanation_failure: null,
    needs_input: null,
    errors: [],
  };
}

function withoutResult(taskStatus: TaskStatus): TimelineToolTaskItem {
  const taskItem = item(taskStatus);
  taskItem.task.selected_tool_run_id = null;
  taskItem.task.selected_result_id = null;
  taskItem.result = null;
  taskItem.assets = [];
  taskItem.explanation = null;
  taskItem.tool_runs = {
    attempt_count: 0,
    selected_tool_run: null,
    has_history: false,
  };
  return taskItem;
}

function mountCard(
  taskItem: TimelineToolTaskItem,
  mutationBusy = false,
) {
  return mount(ToolTaskCard, {
    props: {
      item: taskItem,
      conversationId: "conversation-1",
      mutationBusy,
    },
  });
}

function expectNoRawStatus(wrapper: ReturnType<typeof mountCard>): void {
  expect(wrapper.html()).not.toMatch(rawStatusPattern);
  expect(
    Object.values(wrapper.get(".status-badge").attributes()).join(" "),
  ).not.toMatch(rawStatusPattern);
}

describe("ToolTaskCard", () => {
  it("presents a complete image result in user-facing order without request or technical facts", () => {
    const wrapper = mountCard(item());
    const html = wrapper.html();

    expect(wrapper.attributes("aria-label")).toBe("材料实验结果");
    expect(wrapper.get(".eyebrow").text()).toBe("实验结果");
    expect(wrapper.get("h2").text()).toBe("组织图像已生成");
    expect(wrapper.get(".status-badge").text()).toBe("已完成");
    expect(html.indexOf("<header")).toBeLessThan(html.indexOf('data-section="image"'));
    expect(html.indexOf('data-section="image"')).toBeLessThan(html.indexOf('data-section="conditions"'));
    expect(html.indexOf('data-section="conditions"')).toBeLessThan(html.indexOf('data-section="metrics"'));
    expect(html.indexOf('data-section="metrics"')).toBeLessThan(html.indexOf('data-section="explanation"'));
    expect(wrapper.text()).toContain("1020 °C");
    expect(wrapper.text()).toContain("3.46 %");
    expect(wrapper.text()).toContain("650.10 MPa");
    expect(wrapper.text()).toContain("这是已持久化的成功说明。");
    expect(wrapper.text()).not.toContain("初始请求");
    expect(wrapper.text()).not.toContain("生成组织图像并预测性能");
    expect(wrapper.text()).not.toContain("补充与追问");
    expect(wrapper.text()).not.toMatch(/SUCCEEDED|ToolRun|diagnostic|attempt|duration/i);
    expectNoRawStatus(wrapper);
  });

  it("presents a mechanical-only result without an image section", () => {
    const taskItem = item();
    taskItem.result = result({
      requested_outputs: ["mechanical_properties"],
      completed_outputs: ["mechanical_properties"],
    });
    taskItem.assets = [];
    const wrapper = mountCard(taskItem);

    expect(wrapper.get("h2").text()).toBe("性能预测已完成");
    expect(wrapper.find('[data-section="image"]').exists()).toBe(false);
    expect(wrapper.get('[data-section="metrics"]').text()).toContain("屈服强度");
  });

  it("keeps a successful Result in progress while its Explanation is being generated", () => {
    const taskItem = item("RUNNING");
    taskItem.explanation = null;
    const wrapper = mountCard(taskItem);

    expect(wrapper.get("h2").text()).toBe("正在生成组织图像…");
    expect(wrapper.get(".status-badge").text()).toBe("生成中");
    expect(wrapper.get(".status-badge").attributes("data-tone")).toBe("pending");
    expect(wrapper.find('[data-section="image"]').exists()).toBe(true);
    expect(wrapper.find('[data-section="metrics"]').exists()).toBe(true);
    expect(wrapper.get('[data-section="explanation"]').text()).toContain("结果说明生成中…");
    expect(wrapper.get('[data-section="explanation"]').text()).not.toContain("结果说明暂不可用。");
    expect(wrapper.find('[data-action="retry-explanation"]').exists()).toBe(false);
    expectNoRawStatus(wrapper);
  });

  it.each([
    {
      name: "partial",
      taskStatus: "PARTIALLY_SUCCEEDED" as const,
      resultValue: result({
        status: "PARTIALLY_SUCCEEDED",
        completed_outputs: ["sem_image"],
        failed_outputs: ["mechanical_properties"],
      }),
      title: "部分结果已生成",
    },
    {
      name: "complete",
      taskStatus: "SUCCEEDED" as const,
      resultValue: result(),
      title: "组织图像已生成",
    },
  ])(
    "treats a missing Explanation without failure evidence as pending for a $name Result",
    ({ taskStatus, resultValue, title }) => {
      const taskItem = item(taskStatus);
      taskItem.result = resultValue;
      taskItem.explanation = null;
      taskItem.latest_explanation_failure = null;
      const wrapper = mountCard(taskItem);

      expect(wrapper.get("h2").text()).toBe(title);
      expect(wrapper.get('[data-section="explanation"]').text()).toContain(
        "结果说明生成中…",
      );
      expect(wrapper.get('[data-section="explanation"]').text()).not.toContain(
        "结果说明暂不可用。",
      );
      expect(
        wrapper.find('[data-action="retry-explanation"]').exists(),
      ).toBe(false);
    },
  );

  it.each<TaskStatus>(["PENDING", "RUNNING"])(
    "keeps the initial request visible while %s without exposing the enum",
    (status) => {
      const wrapper = mountCard(withoutResult(status));

      expect(wrapper.get("h2").text()).toBe("正在生成组织图像…");
      expect(wrapper.get(".status-badge").text()).toBe("生成中");
      expect(wrapper.text()).toContain("初始请求");
      expect(wrapper.text()).toContain("生成组织图像并预测性能");
      expect(wrapper.text()).not.toContain(status);
      expectNoRawStatus(wrapper);
    },
  );

  it("localizes NEEDS_INPUT fields and emits a localized supplement summary", async () => {
    const taskItem = withoutResult("NEEDS_INPUT");
    taskItem.needs_input = {
      missing_fields: [
        "material",
        "solution_temperature",
        "aging_temperature",
        "aging_time",
      ],
      ambiguous_fields: [
        {
          field: "solution_time",
          message: "请确认单位。",
          private_value: "不应显示",
        },
      ],
      normalized_input: {
        solution_temperature: { value: 1020, unit: "°C" },
        private_payload: "不应显示的 normalized_input",
      },
      candidate_tool_refs: [],
    };
    const wrapper = mountCard(taskItem);

    expect(wrapper.get("h2").text()).toBe("需要补充信息");
    expect(wrapper.get(".status-badge").text()).toBe("待补充");
    expect(wrapper.text()).toContain("材料");
    expect(wrapper.text()).toContain("固溶温度");
    expect(wrapper.text()).toContain("时效温度");
    expect(wrapper.text()).toContain("时效时间");
    expect(wrapper.text()).toContain("固溶时间：请确认单位。");
    expect(wrapper.text()).not.toMatch(/NEEDS_INPUT|material|aging_temperature|aging_time|solution_time/);
    expect(wrapper.text()).not.toContain("normalized_input");
    expect(wrapper.text()).not.toContain("1020");
    expect(wrapper.text()).not.toContain("不应显示");
    expectNoRawStatus(wrapper);

    await wrapper.get('[data-action="supplement"]').trigger("click");
    expect(wrapper.emitted("supplement")).toEqual([
      [
        {
          conversationId: "conversation-1",
          taskId: "task-1",
          summary: "待补充：材料、固溶温度、时效温度、时效时间",
        },
      ],
    ]);
  });

  it("uses a concise localized supplement summary when no missing field is listed", async () => {
    const taskItem = withoutResult("NEEDS_INPUT");
    taskItem.needs_input = {
      missing_fields: [],
      ambiguous_fields: [],
      normalized_input: null,
      candidate_tool_refs: [],
    };
    const wrapper = mountCard(taskItem);

    await wrapper.get('[data-action="supplement"]').trigger("click");
    expect(wrapper.emitted("supplement")).toEqual([
      [
        {
          conversationId: "conversation-1",
          taskId: "task-1",
          summary: "需要补充信息",
        },
      ],
    ]);
  });

  it("keeps a partial image result and offers a localized Tool retry", async () => {
    const taskItem = item("PARTIALLY_SUCCEEDED");
    taskItem.result = result({
      status: "PARTIALLY_SUCCEEDED",
      completed_outputs: ["sem_image"],
      failed_outputs: ["mechanical_properties"],
      error: { safe_message: "性能预测暂不可用。", code: "PRIVATE_RESULT_CODE" },
    });
    const wrapper = mountCard(taskItem);

    expect(wrapper.get("h2").text()).toBe("部分结果已生成");
    expect(wrapper.get(".status-badge").text()).toBe("部分完成");
    expect(wrapper.find('[data-section="image"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("未能生成：力学性能");
    expect(wrapper.text()).toContain("性能预测暂不可用。");
    expect(wrapper.text()).not.toContain("PRIVATE_RESULT_CODE");
    expect(wrapper.get('[data-action="retry-tool"]').text()).toBe("重新生成结果");
    expectNoRawStatus(wrapper);

    await wrapper.get('[data-action="retry-tool"]').trigger("click");
    expect(wrapper.emitted("retry-tool")).toEqual([["task-1"]]);
  });

  it("keeps a complete result successful when only its explanation failed", async () => {
    const taskItem = item("PARTIALLY_SUCCEEDED");
    taskItem.explanation = explanation("FAILED");
    const wrapper = mountCard(taskItem);

    expect(wrapper.get("h2").text()).toBe("组织图像已生成");
    expect(wrapper.get(".status-badge").text()).toBe("已完成");
    expect(wrapper.find('[data-section="image"]').exists()).toBe(true);
    expect(wrapper.find('[data-section="metrics"]').exists()).toBe(true);
    expect(wrapper.get('[data-section="explanation"]').text()).toContain("结果说明暂不可用。");
    expect(wrapper.get('[data-section="explanation"]').text()).toContain("说明服务暂时不可用。");
    expect(wrapper.find('[data-action="retry-tool"]').exists()).toBe(false);
    expect(wrapper.get('[data-action="retry-explanation"]').text()).toBe("重新生成说明");
    expectNoRawStatus(wrapper);

    await wrapper.get('[data-action="retry-explanation"]').trigger("click");
    expect(wrapper.emitted("retry-explanation")).toEqual([["result-1"]]);
  });

  it("shows only safe errors and the request when a Task failed without a result", async () => {
    const taskItem = withoutResult("FAILED");
    taskItem.task.error_code = "PRIVATE_TASK_CODE";
    taskItem.task.safe_error_message = "组织图像生成暂时失败。";
    taskItem.errors = [
      { code: "PRIVATE_ITEM_CODE", message: "请稍后重新生成。" },
    ];
    taskItem.tool_runs = {
      attempt_count: 1,
      selected_tool_run: toolRun("FAILED"),
      has_history: true,
    };
    const wrapper = mountCard(taskItem);

    expect(wrapper.get("h2").text()).toBe("生成失败");
    expect(wrapper.get(".status-badge").text()).toBe("失败");
    expect(wrapper.text()).toContain("初始请求");
    expect(wrapper.text()).toContain("组织图像生成暂时失败。");
    expect(wrapper.text()).toContain("请稍后重新生成。");
    expect(wrapper.text()).not.toMatch(/FAILED|PRIVATE_TASK_CODE|PRIVATE_ITEM_CODE|PRIVATE_TOOL_RUN_CODE/);
    expect(wrapper.text()).not.toContain("不应显示的 ToolRun 错误。");
    expect(wrapper.get('[data-action="retry-tool"]').text()).toBe("重新生成结果");
    expectNoRawStatus(wrapper);

    await wrapper.get('[data-action="retry-tool"]').trigger("click");
    expect(wrapper.emitted("retry-tool")).toEqual([["task-1"]]);
  });

  it("disables every visible mutation action while another mutation is active", () => {
    const needsInput = withoutResult("NEEDS_INPUT");
    needsInput.needs_input = {
      missing_fields: ["material"],
      ambiguous_fields: [],
      normalized_input: null,
      candidate_tool_refs: [],
    };
    expect(mountCard(needsInput, true).get('[data-action="supplement"]').attributes()).toHaveProperty("disabled");

    const partial = item("PARTIALLY_SUCCEEDED");
    partial.result = result({
      status: "PARTIALLY_SUCCEEDED",
      completed_outputs: ["sem_image"],
      failed_outputs: ["mechanical_properties"],
    });
    partial.latest_explanation_failure = explanation("FAILED", 2);
    const wrapper = mountCard(partial, true);
    expect(wrapper.get('[data-action="retry-tool"]').attributes()).toHaveProperty("disabled");
    expect(wrapper.get('[data-action="retry-explanation"]').attributes()).toHaveProperty("disabled");
  });

  it("keeps successful explanation text above the latest regeneration failure", () => {
    const taskItem = item("PARTIALLY_SUCCEEDED");
    taskItem.latest_explanation_failure = explanation("FAILED", 2);
    const wrapper = mountCard(taskItem);
    const explanationSection = wrapper.get('[data-section="explanation"]');

    expect(wrapper.get(".status-badge").text()).toBe("已完成");
    expect(explanationSection.text()).toContain("这是已持久化的成功说明。");
    expect(explanationSection.text()).toContain("最近一次重新生成说明失败");
    expect(explanationSection.text()).toContain("说明服务暂时不可用。");
    expect(explanationSection.text().indexOf("这是已持久化的成功说明。")).toBeLessThan(
      explanationSection.text().indexOf("最近一次重新生成说明失败"),
    );
  });

  it("never exposes ToolRun errors, raw statuses, unknown objects, or private JSON", () => {
    const taskItem = item();
    taskItem.task.safe_error_message = "任务安全提示。";
    taskItem.errors = [{ code: "PRIVATE_ITEM_CODE", message: "结果安全提示。" }];
    taskItem.result = result({
      warnings: [{ unknown: "unknown warning body", private_payload: "private warning" }],
      error: { unknown: "raw error body", code: "PRIVATE_RESULT_CODE" },
    });
    const text = mountCard(taskItem).text();

    expect(text).toContain("任务安全提示。");
    expect(text).toContain("结果安全提示。");
    expect(text).not.toMatch(/SUCCEEDED|PARTIALLY_SUCCEEDED|FAILED|PENDING|RUNNING|NEEDS_INPUT/);
    expect(text).not.toMatch(/private-tool-run-id|private-tool-version|private-schema-version/);
    expect(text).not.toMatch(/private-diagnostic-step|PRIVATE_DIAGNOSTIC_CODE|不应显示的 ToolRun/);
    expect(text).not.toMatch(/raw_json|不应显示的原始结果|不应显示的来源数据/);
    expect(text).not.toMatch(/unknown warning body|private warning|raw error body|PRIVATE_RESULT_CODE|PRIVATE_ITEM_CODE/);
    expect(text).not.toContain("[object Object]");
    expect(text).not.toContain("运行历史");
  });
});
