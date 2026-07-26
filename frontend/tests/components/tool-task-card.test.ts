import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ToolTaskCard from "../../src/components/ToolTaskCard.vue";
import type {
  AssetSummary,
  ExplanationSummary,
  ResultSummary,
  TaskDetail,
  TaskStatus,
  TimelineToolTaskItem,
  ToolRunSummary,
} from "../../src/api/types";

const timestamp = "2026-07-24T12:00:00Z";

function run(
  attemptNo: number,
  status: ToolRunSummary["status"] = "SUCCEEDED",
  selected = false,
): ToolRunSummary {
  return {
    tool_run_id: `run-${attemptNo}`,
    attempt_no: attemptNo,
    tool_id: "zta35g_sem_virtual_lab",
    tool_version: "0.1.0",
    schema_version: "1.0",
    status,
    requested_outputs: ["sem_image", "mechanical_properties"],
    completed_outputs:
      status === "SUCCEEDED"
        ? ["sem_image", "mechanical_properties"]
        : ["sem_image"],
    failed_outputs:
      status === "SUCCEEDED" ? [] : ["mechanical_properties"],
    created_at: timestamp,
    started_at: timestamp,
    completed_at: timestamp,
    duration_ms: attemptNo * 100,
    diagnostics_summary: [
      {
        step: "sem_generation",
        status: "SUCCEEDED",
        duration_ms: 42,
        error_code: null,
        safe_error_message: null,
      },
    ],
    error:
      status === "SUCCEEDED"
        ? null
        : {
            code: "SAFE_TOOL_FAILURE",
            message: "工具未完成全部输出。",
          },
    is_selected: selected,
  };
}

function result(
  status: ResultSummary["status"] = "SUCCEEDED",
): ResultSummary {
  return {
    result_id: "result-1",
    tool_run_id: "run-2",
    status,
    requested_outputs: ["sem_image", "mechanical_properties"],
    completed_outputs:
      status === "SUCCEEDED"
        ? ["sem_image", "mechanical_properties"]
        : ["sem_image"],
    failed_outputs:
      status === "SUCCEEDED" ? [] : ["mechanical_properties"],
    data: {
      yield_strength: {
        value: 650,
        unit: "MPa",
      },
      elongation: 3.2,
      verified: true,
      optional: null,
      series: [1, 2, 3],
    },
    warnings: [
      {
        code: "MODEL_EVALUATION_INCOMPLETE",
        message: "模型评价仍需补充。",
        private_payload: "不得显示",
      },
      { arbitrary: "unknown warning body" },
    ],
    provenance: {
      task_id: "task-1",
      tool_run_id: "run-2",
      input_revision: 2,
    },
    error:
      status === "SUCCEEDED"
        ? null
        : {
            code: "MECHANICAL_PROPERTY_PREDICTION_FAILED",
            safe_message: "力学性能预测未完成。",
            private_detail: "不得显示",
          },
    tool_id: "zta35g_sem_virtual_lab",
    tool_version: "0.1.0",
    schema_version: "1.0",
    created_at: timestamp,
  };
}

function asset(
  contentUrl = "/api/v1/assets/asset-1/content",
): AssetSummary {
  return {
    asset_id: "asset-1",
    status: "AVAILABLE",
    role: "requested_output",
    media_type: "image/png",
    width: 512,
    height: 512,
    bit_depth: 8,
    size_bytes: 2048,
    sha256: "a".repeat(64),
    content_url: contentUrl,
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
    text: status === "SUCCEEDED" ? "这是已持久化的成功解释。" : null,
    completed_at: timestamp,
    duration_ms: 80,
    error_code: status === "FAILED" ? "EXPLANATION_FAILED" : null,
    safe_error_message:
      status === "FAILED" ? "解释服务暂时不可用。" : null,
  };
}

function item(
  taskStatus: TaskStatus = "SUCCEEDED",
): TimelineToolTaskItem {
  return {
    item_type: "TOOL_TASK",
    item_id: "task-1",
    task_id: "task-1",
    anchor_at: timestamp,
    initial_user_message: {
      message_id: "message-initial",
      role: "USER",
      content_text: "生成 SEM 图像并预测性能",
      created_at: timestamp,
    },
    task: {
      task_id: "task-1",
      task_type: "TOOL_EXECUTION",
      status: taskStatus,
      selected_tool_run_id: "run-2",
      selected_result_id: "result-1",
      created_at: timestamp,
      started_at: timestamp,
      updated_at: timestamp,
      completed_at: timestamp,
      error_code: null,
      safe_error_message: null,
    },
    input_thread: [],
    input_thread_count: 0,
    input_thread_truncated: false,
    tool_runs: {
      attempt_count: 2,
      selected_tool_run: run(2, "SUCCEEDED", true),
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

function detail(toolRuns: ToolRunSummary[]): TaskDetail {
  return {
    task_id: "task-1",
    conversation_id: "conversation-1",
    task_type: "TOOL_EXECUTION",
    status: "SUCCEEDED",
    anchor_at: timestamp,
    selected_tool_run_id: "run-2",
    selected_result_id: "result-1",
    created_at: timestamp,
    started_at: timestamp,
    updated_at: timestamp,
    completed_at: timestamp,
    error_code: null,
    safe_error_message: null,
    needs_input: null,
    tool_run_count: toolRuns.length,
    tool_runs: toolRuns,
    selected_result_summary: {
      result_id: "result-1",
      status: "SUCCEEDED",
      requested_outputs: ["sem_image"],
      completed_outputs: ["sem_image"],
      failed_outputs: [],
    },
    assets: [asset()],
    explanation_summary: explanation(),
    latest_explanation_failure: null,
  };
}

function mountCard(
  taskItem: TimelineToolTaskItem,
  overrides: Partial<{
    mutationBusy: boolean;
    taskDetail: TaskDetail;
    historyLoading: boolean;
  }> = {},
) {
  return mount(ToolTaskCard, {
    props: {
      item: taskItem,
      conversationId: "conversation-1",
      mutationBusy: false,
      historyLoading: false,
      ...overrides,
    },
  });
}

describe("ToolTaskCard", () => {
  it("shows NEEDS_INPUT facts and emits only the explicitly clicked target", async () => {
    const needsInput = item("NEEDS_INPUT");
    needsInput.task.selected_tool_run_id = null;
    needsInput.task.selected_result_id = null;
    needsInput.tool_runs = {
      attempt_count: 0,
      selected_tool_run: null,
      has_history: false,
    };
    needsInput.result = null;
    needsInput.assets = [];
    needsInput.explanation = null;
    needsInput.needs_input = {
      missing_fields: ["aging_temperature", "aging_time"],
      ambiguous_fields: [
        {
          field: "solution_time",
          message: "请确认单位。",
          private_value: "不得显示",
        },
      ],
      normalized_input: {
        solution_temperature: "1000 °C",
      },
    };
    const wrapper = mountCard(needsInput);

    expect(wrapper.text()).toContain("等待补充");
    expect(wrapper.text()).toContain("aging_temperature");
    expect(wrapper.text()).toContain("请确认单位");
    expect(wrapper.text()).toContain("solution_temperature");
    expect(wrapper.emitted("supplement")).toBeUndefined();

    await wrapper.get("[data-action=supplement]").trigger("click");
    expect(wrapper.emitted("supplement")).toEqual([
      [
        {
          conversationId: "conversation-1",
          taskId: "task-1",
          summary: "待补充：aging_temperature、aging_time",
        },
      ],
    ]);
  });

  it("renders bounded nested normalized input values instead of hiding plain records", () => {
    const needsInput = item("NEEDS_INPUT");
    needsInput.task.selected_tool_run_id = null;
    needsInput.task.selected_result_id = null;
    needsInput.tool_runs = {
      attempt_count: 0,
      selected_tool_run: null,
      has_history: false,
    };
    needsInput.result = null;
    needsInput.assets = [];
    needsInput.explanation = null;
    needsInput.needs_input = {
      missing_fields: ["composition"],
      ambiguous_fields: [],
      normalized_input: {
        process_parameters: {
          solution_temperature: {
            value: 1000,
            unit: "°C",
          },
          aging_time: {
            value: 3,
            unit: "h",
          },
          too_deep: {
            level_two: {
              level_three: {
                private_value: "不得展开",
              },
            },
          },
        },
        requested_outputs: ["sem_image", "mechanical_properties"],
        unsupported_function: () => "不得调用",
        unsupported_special_object: new Date("2026-07-24T00:00:00Z"),
      },
    };

    const wrapper = mountCard(needsInput);

    expect(wrapper.text()).toContain("process_parameters");
    expect(wrapper.text()).toContain("solution_temperature");
    expect(wrapper.text()).toContain("1000");
    expect(wrapper.text()).toContain("°C");
    expect(wrapper.text()).toContain("aging_time");
    expect(wrapper.text()).toContain("3");
    expect(wrapper.text()).toContain("h");
    expect(wrapper.text()).toContain("sem_image");
    expect(wrapper.text()).toContain("mechanical_properties");
    expect(wrapper.text()).toContain("嵌套内容未展开");
    expect(wrapper.text()).not.toContain("不得展开");
    expect(wrapper.text()).not.toContain("不得调用");
    expect(wrapper.text()).not.toContain("已提供结构化值");
    expect(wrapper.find("pre").exists()).toBe(false);
  });

  it("keeps input thread Backend order and marks truncation", () => {
    const taskItem = item();
    taskItem.input_thread = [
      {
        message_id: "later-time-first",
        role: "ASSISTANT",
        content_text: "第一条追问",
        created_at: "2026-07-24T18:00:00Z",
      },
      {
        message_id: "earlier-time-second",
        role: "USER",
        content_text: "第二条补充",
        created_at: "2026-07-24T08:00:00Z",
      },
    ];
    taskItem.input_thread_count = 8;
    taskItem.input_thread_truncated = true;
    const wrapper = mountCard(taskItem);

    const messages = wrapper.findAll("[data-input-message]");
    expect(messages.map((entry) => entry.text())).toEqual([
      expect.stringContaining("第一条追问"),
      expect.stringContaining("第二条补充"),
    ]);
    expect(wrapper.text()).toContain(
      "较早的部分补充记录未在当前卡片中展开",
    );
  });

  it("renders structured Result fields without dumping unknown objects", () => {
    const wrapper = mountCard(item());

    expect(wrapper.text()).toContain("650");
    expect(wrapper.text()).toContain("MPa");
    expect(wrapper.text()).toContain("模型评价仍需补充");
    expect(wrapper.text()).toContain("结果包含一项附加提示");
    expect(wrapper.text()).toContain("zta35g_sem_virtual_lab");
    expect(wrapper.text()).not.toContain("private_payload");
    expect(wrapper.text()).not.toContain("unknown warning body");
    expect(wrapper.find("pre").exists()).toBe(false);
  });

  it("uses public inline and attachment URLs and isolates image failure", async () => {
    const wrapper = mountCard(item());
    const image = wrapper.get("img");
    const download = wrapper.get("a[download]");

    expect(image.attributes("src")).toBe(
      "/api/v1/assets/asset-1/content",
    );
    expect(image.attributes("alt")).toBe("生成的 SEM 图像");
    expect(download.attributes("href")).toBe(
      "/api/v1/assets/asset-1/content?disposition=attachment",
    );
    expect(wrapper.text()).toContain("650");
    expect(wrapper.text()).toContain("这是已持久化的成功解释");

    await image.trigger("error");
    expect(wrapper.text()).toContain("图片加载失败");
    expect(wrapper.text()).toContain("650");
    expect(wrapper.text()).toContain("这是已持久化的成功解释");
  });

  it("remounts the same public image URL after an explicit local reload", async () => {
    const wrapper = mountCard(item());
    const firstImage = wrapper.get("img");
    const firstElement = firstImage.element;

    await firstImage.trigger("error");
    expect(wrapper.text()).toContain("图片加载失败");
    expect(wrapper.text()).toContain("650");
    expect(wrapper.text()).toContain("这是已持久化的成功解释");

    await wrapper.get("[data-action=reload-image]").trigger("click");
    const reloadedImage = wrapper.get("img");
    expect(reloadedImage.element).not.toBe(firstElement);
    expect(reloadedImage.attributes("src")).toBe(
      "/api/v1/assets/asset-1/content",
    );
    expect(wrapper.text()).toContain("图片加载中");

    await reloadedImage.trigger("load");
    expect(wrapper.text()).not.toContain("图片加载失败");
    expect(wrapper.text()).not.toContain("图片加载中");
    expect(wrapper.text()).toContain("650");
    expect(wrapper.text()).toContain("这是已持久化的成功解释");
  });

  it("does not render an image or download link for an invalid public URL", () => {
    const taskItem = item();
    taskItem.assets = [asset("https://storage.invalid/private/image.png")];
    const wrapper = mountCard(taskItem);

    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.find("a[download]").exists()).toBe(false);
    expect(wrapper.find("[data-action=reload-image]").exists()).toBe(false);
    expect(wrapper.text()).toContain("图片地址不可用");
    expect(wrapper.text()).toContain("650");
  });

  it("keeps a successful Explanation above the latest retry failure", () => {
    const taskItem = item();
    taskItem.latest_explanation_failure = explanation("FAILED", 2);
    const wrapper = mountCard(taskItem);

    expect(wrapper.text()).toContain("这是已持久化的成功解释");
    expect(wrapper.text()).toContain("最近一次解释重试失败");
    expect(wrapper.text()).toContain("解释服务暂时不可用");
  });

  it.each<TaskStatus>([
    "PENDING",
    "RUNNING",
    "NEEDS_INPUT",
    "SUCCEEDED",
  ])("does not offer Tool retry for Task status %s", (status) => {
    const taskItem = item(status);
    taskItem.result = status === "SUCCEEDED" ? result() : null;
    const wrapper = mountCard(taskItem);

    expect(wrapper.find("[data-action=retry-tool]").exists()).toBe(false);
  });

  it("offers Tool retry only for failed or partial result paths", async () => {
    const failedWithoutResult = item("FAILED");
    failedWithoutResult.result = null;
    failedWithoutResult.assets = [];
    failedWithoutResult.explanation = null;
    failedWithoutResult.task.selected_result_id = null;
    const failed = mountCard(failedWithoutResult);
    await failed.get("[data-action=retry-tool]").trigger("click");
    expect(failed.emitted("retry-tool")).toEqual([["task-1"]]);

    const partial = item("PARTIALLY_SUCCEEDED");
    partial.result = result("PARTIALLY_SUCCEEDED");
    const partialWrapper = mountCard(partial);
    expect(partialWrapper.find("[data-action=retry-tool]").exists()).toBe(
      true,
    );

    const explanationOnlyFailure = item("PARTIALLY_SUCCEEDED");
    explanationOnlyFailure.result = result("SUCCEEDED");
    explanationOnlyFailure.explanation = explanation("FAILED");
    const explanationOnly = mountCard(explanationOnlyFailure);
    expect(explanationOnly.find("[data-action=retry-tool]").exists()).toBe(
      false,
    );
  });

  it("offers Explanation retry only when Result exists and explanation needs it", async () => {
    const missing = item("PARTIALLY_SUCCEEDED");
    missing.explanation = null;
    const missingWrapper = mountCard(missing);
    await missingWrapper
      .get("[data-action=retry-explanation]")
      .trigger("click");
    expect(missingWrapper.emitted("retry-explanation")).toEqual([
      ["result-1"],
    ]);

    const successful = mountCard(item());
    expect(
      successful.find("[data-action=retry-explanation]").exists(),
    ).toBe(false);

    const latestFailure = item();
    latestFailure.latest_explanation_failure = explanation("FAILED", 2);
    expect(
      mountCard(latestFailure)
        .find("[data-action=retry-explanation]")
        .exists(),
    ).toBe(true);

    const withoutResult = item("FAILED");
    withoutResult.result = null;
    withoutResult.explanation = null;
    expect(
      mountCard(withoutResult)
        .find("[data-action=retry-explanation]")
        .exists(),
    ).toBe(false);
  });

  it("disables retry and supplement actions while a mutation is active", () => {
    const taskItem = item("PARTIALLY_SUCCEEDED");
    taskItem.result = result("PARTIALLY_SUCCEEDED");
    taskItem.latest_explanation_failure = explanation("FAILED", 2);
    const wrapper = mountCard(taskItem, { mutationBusy: true });

    expect(
      wrapper.get("[data-action=retry-tool]").attributes(),
    ).toHaveProperty("disabled");
    expect(
      wrapper.get("[data-action=retry-explanation]").attributes(),
    ).toHaveProperty("disabled");
  });

  it("loads history only on demand and preserves Backend run order", async () => {
    const wrapper = mountCard(item());

    expect(wrapper.emitted("load-history")).toBeUndefined();
    await wrapper.get("[data-action=load-history]").trigger("click");
    expect(wrapper.emitted("load-history")).toEqual([["task-1"]]);

    await wrapper.setProps({
      taskDetail: detail([
        run(2, "SUCCEEDED", true),
        run(1, "FAILED", false),
      ]),
    });
    expect(
      wrapper
        .findAll("[data-history-attempt]")
        .map((entry) => entry.attributes("data-history-attempt")),
    ).toEqual(["2", "1"]);
    expect(wrapper.text()).toContain("当前选中");

    await wrapper.get("[data-action=close-history]").trigger("click");
    await wrapper.get("[data-action=load-history]").trigger("click");
    expect(wrapper.emitted("load-history")).toHaveLength(1);
  });

  it("shows safe Task and ToolRun errors without internal object dumps", () => {
    const taskItem = item("FAILED");
    taskItem.task.safe_error_message = "任务安全错误。";
    taskItem.errors = [
      { code: "SAFE_ERROR", message: "公共错误摘要。" },
    ];
    taskItem.tool_runs.selected_tool_run = run(2, "FAILED", true);
    const wrapper = mountCard(taskItem);

    expect(wrapper.text()).toContain("任务安全错误");
    expect(wrapper.text()).toContain("公共错误摘要");
    expect(wrapper.text()).toContain("工具未完成全部输出");
    expect(wrapper.text()).not.toContain("[object Object]");
  });
});
