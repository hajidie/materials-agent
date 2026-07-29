# M12-B 真实 LLM Provider 浏览器验收报告

状态：
`M12B_PROJECT_OWNER_ACCEPTED`

本报告记录项目负责人已经在浏览器和数据库中人工核实的 M12-B 正常链路事实。
本轮最终收尾没有再次调用 DeepSeek，也没有采用自动 real-provider Runner。

## 1. Git baseline

- Branch: `main`
- HEAD: `ffd29353cb4682c74fd3455425822999444bb1d2`
- Subject: `feat: add offline DeepSeek provider integration`
- Parent: `f5e24dcaab4801dbeffb8400f2960c33b60b4f00`
- 最终审查范围：2 个 tracked modified 路径与 1 个 untracked 路径
- Staging: empty

## 2. M12-A commit

- Commit: `ffd29353cb4682c74fd3455425822999444bb1d2`
- M12-A 状态：`COMPLETE / PROJECT_OWNER_ACCEPTED`
- M12-A 离线测试继续作为 Adapter contract、错误分类、幂等、配置与应用接线
  的自动化证据。

## 3. Provider、model 与配置

- Provider: `deepseek`
- Model: `deepseek-v4-flash`
- Base URL: `https://api.deepseek.com`
- Temperature: `0`
- Thinking mode: `disabled`
- Streaming: `false`
- Automatic retries: `0`
- Chat max tokens: `1024`
- Explanation max tokens: `768`

## 4. 浏览器验收环境

- 使用正常本地开发栈。
- Backend 使用 DeepSeek Provider。
- PostgreSQL 与 MinIO 使用本地服务。
- Tool Runtime 使用 Mock Runtime。
- Frontend 浏览器是主要人工业务验收入口。
- 数据库 `LLMCall` 审计是 Provider metadata 的主要证据。
- M12-A 离线测试是错误映射与幂等合同证据。
- 未运行真实 SEM 模型。
- 未采用、未提交自动 real-provider Runner。

## 5. 知识问答结果

- 浏览器请求成功。
- 返回真实自然语言材料知识回答。
- 未触发 Tool。

## 6. NEEDS_INPUT 结果

- 浏览器请求成功。
- `missing_fields`：
  - `aging_temperature`
  - `aging_time`
- 未执行 Tool。

## 7. 分钟单位规范化结果

- 浏览器请求成功。
- 输入中的 `90 min` 由 Application 规范化为 `1.5 h`。
- `missing_fields`：
  - `aging_time`
- 最终保持 `NEEDS_INPUT`。
- 未执行 Tool。

## 8. 完整 Tool 结果

- Task 最终状态：`SUCCEEDED`
- 图片：存在
- 力学性能结果：存在
- 自动 Explanation：存在
- 页面安全错误：无
- Tool Runtime：Mock Runtime
- 真实 SEM 模型：未运行

## 9. 自动 Explanation

- 完整 Tool 正常 Application 链路自动生成 Explanation。
- Explanation 已存在并与该次 Tool 结果关联。
- 未通过直接 Adapter 调用构造验收证据。

## 10. LLMCall metadata 审计

- DeepSeek `LLMCall` 总数：`6`
- `SUCCEEDED`：`6`
- `CHAT_ORCHESTRATION`：`5`
- `TOOL_RESULT_EXPLANATION`：`1`
- 非法 prompt digest：`0`
- 缺失或非法 token usage：`0`
- 带 `error_code` 的记录：`0`
- Provider：全部为 `deepseek`
- Model：全部为 `deepseek-v4-flash`
- Chat template：`chat-orchestration/2`
- Explanation template：`tool-result-explanation/2`
- Prompt digest：全部有效
- `input_tokens` / `output_tokens`：全部有效
- Generation parameters：与 Chat/Explanation 固定配置一致

## 11. 调用次数

- Planned acceptance calls: `5`
- Additional manual knowledge call: `1`
- Observed DeepSeek LLMCalls: `6`
- Call purposes:
  - `CHAT_ORCHESTRATION=5`
  - `TOOL_RESULT_EXPLANATION=1`
- All succeeded: `true`

额外调用来自人工验收过程中用户追加的“你是谁”知识问答。它是实际验收过程偏差，
因此总数如实记录为 6，不改写为计划值 5，也不通过额外调用进行补测。

## 12. x-request-id observation

- `x-request-id` present：`0`
- `x-request-id` absent：`6`
- Provider 未暴露 `x-request-id`，因此 6 条记录的
  `provider_request_id=null`。
- 未使用 completion ID、LangChain ID、Task ID 或应用 request ID 替代。

## 13. 安全边界

本报告只保存业务与 metadata 安全摘要，不保存 API Key、完整 Prompt、完整用户
对话副本、完整 Provider response、原始 `choices`、`reasoning_content` 内容、
完整 response headers、SDK exception body 或本地绝对路径。

安全术语名称和环境变量名称可以用于说明规则，但它们本身不是 Secret。最终三路径
安全扫描只允许规则名称，不允许任何实际凭据值、Authorization header 值或
Bearer token。

## 14. Mock 恢复

- 项目负责人确认本地配置已恢复为 `LLM_ADAPTER=mock`。
- 项目负责人确认 `DEEPSEEK_API_KEY` 已清空。
- 最终收尾不读取 `.env` 内容。
- 最终收尾不进行任何真实 Provider 调用。

## 15. Phase 1A 回归

- Runner：`scripts/acceptance/run-phase-1a.ps1`
- Adapter：强制 Mock
- Run ID：`20260729T095845Z-1fc7cbda8a8d`
- Result：`PHASE_1A_AUTOMATION_PASSED`，`failed=0`
- Backend full：`978 passed`
- Backend E2E：`24 passed`，`M11_SCENARIO_COUNT_OK`
- Mock Runtime：`11 passed`
- Frontend：9 个 test files / `204 passed`，typecheck 与 build 退出码均为 0
- Alembic：heads/current 均为 `0009_timeline_query_indexes (head)`，check 退出码为 0
- Scope：`SCOPE_OK M12A`
- SEM：`SEM_INTEGRITY_OK`
- Security scan：`ACCEPTANCE_SECURITY_SCAN_OK`，`dynamic_artifacts_scanned=true`

该 Runner 只证明 Mock 回退和阶段 1A 回归，不是 M12-B 真实 Provider 验收入口，
也不代表自动 real-provider Runner 完成了真实验收。

## 16. Scope / SEM / Git

- Scope M12B：`SCOPE_OK M12B`
- SEM integrity：`SEM_INTEGRITY_OK`
- Git changed paths：精确 3 路径（2 tracked modified + 1 untracked）
- `git diff --check`：`PASS`
- `git diff --cached --check`：`PASS`
- 三路径安全扫描：`PASS`
- Staging：empty
- Commit：本轮已授权在 staged-diff 门槛通过后创建唯一验收提交
- Push / amend：未授权

## 17. 已知限制

- 本轮真实验收只覆盖正常业务链路；真实错误分类与幂等合同引用 M12-A 离线证据。
- Provider 未暴露 `x-request-id`，这是能力观察，不使用其他标识符替代。
- 实际产生 6 条真实 LLMCall，比计划多 1 条人工知识问答。
- 自动 real-provider Runner 未被采用，两个未跟踪实现文件已从最终范围删除。
- 真实 SEM 模型未运行。
- M12-B 已完成项目负责人最终复审；M13 未开始，真实 SEM 模型仍未运行。

## 18. 最终结论

- 浏览器真实业务验收：`PASSED / PROJECT_OWNER_ACCEPTED`
- 数据库 Provider metadata 审计：`PASSED / PROJECT_OWNER_ACCEPTED`
- Mock 回退与 Phase 1A：`PASSED`
- M12-B：`COMPLETE / PROJECT_OWNER_ACCEPTED`
- M12：`COMPLETE / PROJECT_OWNER_ACCEPTED`
- M13：`NOT STARTED`
- 本轮新增真实 Provider 调用：`0`
- 累计观察到的真实 DeepSeek LLMCalls：`6`

最终状态：
`M12B_PROJECT_OWNER_ACCEPTED`
