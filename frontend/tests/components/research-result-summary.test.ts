import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ResearchResultSummary from "../../src/components/ResearchResultSummary.vue";
import type { ResultSummary } from "../../src/api/types";

const timestamp = "2026-08-04T00:00:00Z";

function result(overrides: Partial<ResultSummary> = {}): ResultSummary {
  return {
    result_id: "result-1",
    tool_run_id: "private-run-id",
    status: "SUCCEEDED",
    requested_outputs: ["mechanical_properties"],
    completed_outputs: ["mechanical_properties"],
    failed_outputs: [],
    data: {
      elongation: { value: 3.456, unit: "%" },
      yield_strength: { value: 650.1, unit: "MPa" },
    },
    warnings: [],
    provenance: {
      normalized_process_parameters: {
        solution_temperature: { value: 1020, unit: "°C" },
        solution_time: { value: 1.5, unit: "h" },
        aging_temperature: { value: 480, unit: "°C" },
        aging_time: { value: 8, unit: "h" },
      },
    },
    error: null,
    tool_id: "zta35g_sem_virtual_lab",
    tool_version: "private-version",
    schema_hash: "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07",
    created_at: timestamp,
    ...overrides,
  };
}

describe("ResearchResultSummary", () => {
  it("shows complete conditions and requested metrics in fixed user-facing order", () => {
    const wrapper = mount(ResearchResultSummary, { props: { result: result() } });

    expect(wrapper.attributes("aria-label")).toBe("研究结果摘要");
    expect(wrapper.get("[data-section=conditions]").text()).toContain("固溶温度");
    expect(wrapper.get("[data-section=conditions]").text()).toContain("1020 °C");
    const metrics = wrapper.get("[data-section=metrics]").text();
    expect(metrics).toContain("延伸率");
    expect(metrics).toContain("3.46 %");
    expect(metrics).toContain("屈服强度");
    expect(metrics).toContain("650.10 MPa");
    expect(metrics.indexOf("延伸率")).toBeLessThan(metrics.indexOf("屈服强度"));
  });

  it("does not show a metrics section when only an image was completed", () => {
    const wrapper = mount(ResearchResultSummary, {
      props: {
        result: result({
          requested_outputs: ["sem_image"],
          completed_outputs: ["sem_image"],
        }),
      },
    });

    expect(wrapper.find("[data-section=metrics]").exists()).toBe(false);
  });

  it("uses neutral fallbacks without leaking invalid, unknown, or private values", () => {
    const wrapper = mount(ResearchResultSummary, {
      props: {
        result: result({
          completed_outputs: ["mechanical_properties"],
          data: {
            elongation: { value: "secret", unit: "%", private_payload: "leak-data" },
            yield_strength: { value: 600, unit: 42 },
            arbitrary: { raw_json: "do-not-display" },
          },
          provenance: {
            normalized_process_parameters: {
              solution_temperature: { value: 1020, unit: "°C" },
              solution_time: { value: 1, unit: "h" },
              aging_temperature: { value: 480, unit: "°C" },
              aging_time: { value: Infinity, unit: "h", private_value: "leak-me" },
            },
            tool_run_id: "private-run-id",
          },
        }),
      },
    });

    expect(wrapper.text()).toContain("实验条件暂不可用");
    expect(wrapper.text()).toContain("性能数据暂不可用");
    expect(wrapper.text()).not.toContain("leak-data");
    expect(wrapper.text()).not.toContain("leak-me");
    expect(wrapper.text()).not.toContain("do-not-display");
    expect(wrapper.text()).not.toContain("private-run-id");
    expect(wrapper.text()).not.toContain("private-version");
    expect(wrapper.text()).not.toContain("private-schema");
  });

  it("does not render warnings and shows only an explicit safe error message", () => {
    const wrapper = mount(ResearchResultSummary, {
      props: {
        result: result({
          warnings: [
            { message: "内部告警消息", code: "PRIVATE_WARNING", private: "leak-warning" },
            { safe_message: "未建立安全显示契约", raw: "C:\\private\\model.bin" },
            { unknown: "do-not-display" },
          ],
          error: {
            safe_message: "性能计算暂不可用",
            code: "PRIVATE_ERROR",
            detail: "leak-error",
          },
        }),
      },
    });

    expect(wrapper.text()).toContain("性能计算暂不可用");
    expect(wrapper.find("[aria-label=提示]").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("内部告警消息");
    expect(wrapper.text()).not.toContain("未建立安全显示契约");
    expect(wrapper.text()).not.toContain("C:\\private\\model.bin");
    expect(wrapper.text()).not.toContain("PRIVATE_WARNING");
    expect(wrapper.text()).not.toContain("PRIVATE_ERROR");
    expect(wrapper.text()).not.toContain("leak-warning");
    expect(wrapper.text()).not.toContain("leak-raw");
    expect(wrapper.text()).not.toContain("leak-error");
    expect(wrapper.text()).not.toContain("do-not-display");
  });

  it("does not fall back to an arbitrary error message", () => {
    const wrapper = mount(ResearchResultSummary, {
      props: {
        result: result({
          error: {
            message: "C:\\private\\runtime-error.log",
            code: "PRIVATE_ERROR",
          },
        }),
      },
    });

    expect(wrapper.find("[aria-label=结果错误]").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("C:\\private\\runtime-error.log");
    expect(wrapper.text()).not.toContain("PRIVATE_ERROR");
  });
});
