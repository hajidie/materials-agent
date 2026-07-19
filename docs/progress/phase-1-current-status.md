# 阶段 1 当前执行状态

> 本文件只保存当前状态、最近一次工作单元摘要和下一步。完整历史由未来经项目负责人验收后的 Git commit 保存。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M4 |
| 当前工作单元 | M4-B 业务实现 |
| 状态 | `READY_FOR_M4B` |
| 上一已完成工作单元 | M4-B 计划边界修正 |
| M4-A | 已验收并提交 |
| M4-B 计划边界修正 | 已通过项目负责人验收，等待本次提交 |
| M4-B 业务实现 | 尚未开始 |
| M5 | 尚未开始 |
| 当前 branch / HEAD | `main` / `b50545b27fa71b5297b736b088899559cf3c4079` |
| 已确认设计基线 | 第一节、第二节、第三节、第四节 A、第四节 B，共五份；本轮未修改 |
| 阶段 1 实施计划 | `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`；边界修正已通过项目负责人最终验收，等待本次提交 |
| 是否处于项目负责人暂停点 | 否 |
| 更新时间 | `2026-07-19T21:03:06+08:00` |

## 项目负责人已批准的 M4-B 稳定规则

完整合法 ToolCandidate 在完整 Tool 结果链尚未开放时固定形成：

```text
LLMCall.status = SUCCEEDED
创建 TaskInputRevision revision 1
Task.task_type = TOOL_EXECUTION
Task.current_status = FAILED
Task.error_code = TOOL_UNAVAILABLE
Task.safe_error_message = 固定安全文本
HTTP = 503
不创建 ToolRun
不创建 ToolResult
不创建 Asset
不创建成功 AssistantMessage
selected_tool_run_id = null
selected_result_id = null
```

该路径表示 CHAT_ORCHESTRATION 成功，Application 标准化和硬校验成功，但平台当前尚未开放完整 Tool 执行及结果持久化能力；它不是输入校验失败，也不是 Runtime 已执行后失败。

## 本工作单元完成内容

- 为计划边界修正建立独立 `M4PLAN` 精确 scope；未向未来 M4-B 业务 allowlist 加入阶段计划路径。
- 修订阶段 1 实施计划的里程碑总表、暂停点和 M4–M7 章节。
- M4-B 只完成知识回答、NEEDS_INPUT、硬校验失败、编排失败和完整合法候选的安全不可用映射；不得创建 Registry、ToolRun、Runtime、Asset 或 ToolResult。
- M5 只建立 Registry/Catalog、Mock Runtime、HTTP Client Adapter、MaterialTool、ToolRun 和 ToolExecutionService 内部闭环；不把消息 POST 冒充为完整结果。
- M6 建立 Asset 全生命周期、`.npy` 安全校验、PNG、MinIO 和 Asset 读取；仍不激活消息 POST 的完整 Tool 结果。
- M7 才连接 Result、Explanation、selected references 和稳定 Task 状态，并激活消息 POST 的正式 Tool 成功、部分成功和失败响应。
- 最终复审定向修正：所有里程碑正文中的数字检查点引用均改为 M2/M8/M11/M12/M13/M14/M16 具名引用，避免新增 M4/M7 后发生序号漂移。
- 实际 `phase-1-checklist.md` 已从旧八项同步为 G0、M2、M4、M7、M8、M11、M12、M13、M14、M16 十个正式暂停点；其他验收内容未修改。
- 最终全文复审定向修正：计划开头、全局提交/文件结构规则和末尾均改为长期中性说明；当前执行位置只由本动态状态文件确定，G0 里程碑章节和 M0 对 G0 的依赖保持不变。

`M4PLAN` 精确 allowlist：

- `scripts/dev/check-scope.ps1`
- `docs/acceptance/phase-1-checklist.md`
- `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`
- `docs/progress/phase-1-current-status.md`

M4PLAN 累计工作区只包含以上四个文件；本次最终定向修正只修改阶段 1 实施计划和本动态状态文件，`check-scope.ps1` 与 `phase-1-checklist.md` 保持本次开始时内容不变。没有 Backend 业务代码、测试、migration、Mock Runtime、`AGENTS.md` 或五份设计基线变化。

## 最终验证

- `rg -n '本轮只执行 G0|可以新开 Codex 对话开始 M0|计划结束边界' ...implementation-plan.md`：退出码 1，无匹配。
- G0 章节修改前后 SHA-256 均为 `c20a0160df92d026ecef4dee00ed81b4b23a0956ff869b1279130c1703699313`，章节原文保持不变。
- 本次开始与结束时，`scripts/dev/check-scope.ps1` SHA-256 均为 `0a4aaa625884ab3e9218073a01c6a4425fade95301ef343530f66d2669e5a89d`；`docs/acceptance/phase-1-checklist.md` SHA-256 均为 `5f4d5c01d6dbf2f9db4bc6a67a6d040bc5850f4de53e2b87186fa4275c17f7c9`。
- `powershell -ExecutionPolicy Bypass -File scripts/dev/check-scope.ps1 -Milestone M4PLAN`：`SCOPE_OK M4PLAN`。
- `powershell -ExecutionPolicy Bypass -File scripts/dev/check-sem-integrity.ps1`：`SEM_INTEGRITY_OK`，57 files，`total_size_bytes=2043071133`，aggregate fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- `git diff --check`：退出码 0，无输出。
- `git diff --stat` / `git diff --name-only`：M4PLAN 累计工作区正好包含 `scripts/dev/check-scope.ps1`、`docs/acceptance/phase-1-checklist.md`、阶段 1 实施计划和本动态状态文件。
- `git diff --cached --name-only`：无输出，暂存区为空。
- `git status --short --untracked-files=all`：四个允许文件为未暂存修改，无其他变化。
- branch 为 `main`；HEAD 仍为 `b50545b27fa71b5297b736b088899559cf3c4079`（`b50545b feat: add mock chat orchestration foundation`）。
- 本工作单元没有业务代码变化，因此未启动容器、未运行 Backend 测试。

## 已知风险

- M4-B 实现时必须把 `TOOL_UNAVAILABLE` 与硬校验失败、CHAT_ORCHESTRATION 失败和 Runtime 执行失败分开映射，不能复用错误语义。
- M5/M6 的内部成功事实不足以形成公共完整结果；只有 M7 的 Result/Explanation/selected 引用链持久化并达到稳定 Task 状态后才能解除 M4 的固定映射。
- M4-B 业务实现只能在本验收提交完成后的新独立工作单元开始；本次提交对话不得开始业务实现。

## 下一步

本次只创建 M4-B 计划边界修正的唯一验收提交。提交完成后的下一执行位置为 `READY_FOR_M4B`；M4-B 业务实现尚未开始，必须由新的独立工作单元执行，M5 尚未开始。
