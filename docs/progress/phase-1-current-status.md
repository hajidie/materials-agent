# 阶段 1 当前执行状态

> 本文件只保存当前状态、最近一次工作单元摘要和下一步。完整历史由未来经项目负责人验收后的 Git commit 保存。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M5 |
| 当前工作单元 | M5 业务实现 |
| 状态 | `READY_FOR_M5` |
| 上一已完成工作单元 | M4-B 业务实现 |
| M4 | 已完成 |
| M4-A | 已验收并提交 |
| M4-B 计划边界修正 | 已验收并提交 |
| M4-B 业务实现 | 已通过项目负责人最终验收，并包含在本次验收提交中 |
| M5 | 尚未开始 |
| 当前 branch / HEAD | `main` / `d1194b4c34a36f5f67774e055f0a3d5ff47c83c5`（M4-B 验收提交前基线；M5 开始时必须按实际 `git rev-parse HEAD` 刷新） |
| 已确认设计基线 | 第一节、第二节、第三节、第四节 A、第四节 B，共五份；本轮未修改 |
| 是否处于项目负责人暂停点 | 否 |
| 更新时间 | `2026-07-20T00:04:02+08:00` |

## 上一已完成工作单元摘要

- 复用 M3 `MessageSubmissionService.prepare_submission()`，把已提交 UserMessage 和 PENDING Task 接入新的 `ChatOrchestrationService`。
- 使用准备、启动、终结三个短事务；只在所有 UnitOfWork/Session 关闭后调用一次 Mock Chat Port。
- Application 使用 M4-A 标准化器重新计算 missing、ambiguous、normalized、validation 和 requested outputs，不信任 Mock 自报提示字段。
- 正式持久化 LLMCall 生命周期、知识回答 AssistantMessage 或 revision 1，并原子终结 Task。
- 接入同步 `POST /api/v1/conversations/{conversation_id}/messages`；成功响应和错误资源 ID 均来自已提交事实。
- 保持 `GET /api/v1/tasks/{task_id}` 直接读取 Task，不从 Message、Revision 或 LLMCall 反推。
- 增加条件更新、终态重入、Message.llm_call_id 唯一、(task_id, revision) 唯一和来源链重校验。
- 增加 request_id/task_id/llm_call_id 安全关联日志；日志失败为 best-effort，不改变业务事实。
- 对可替换 Chat Port 的未知异常和非法返回进行安全归一化；Port metadata 在 Service 构造期预检。
- 项目负责人复审定向修正：三类安全异常移入 Domain Port 正式失败契约，Application 完全解除对 Mock Infrastructure 类型的反向依赖；Port 与 Mock 删除无用静态 `provider_request_id`，LLMCall 仍固定保存 `null`。
- 项目负责人复审定向修正：重复读取严格校验失败、Knowledge、NEEDS_INPUT、VALIDATION_FAILED、TOOL_UNAVAILABLE 五类 Task/LLMCall/AssistantMessage/Revision 终态组合；同一 LLMCall 的 Revision 数必须为 0 或正好 1，多条或任何不一致均返回 `RESOURCE_CONFLICT`。
- 定向复审补强：Revision Repository 直接按 `llm_call_id` 读取全部关联 Revision；其他 Task 错绑当前 LLMCall 的 Revision 也会被来源校验拒绝，不再被 task-first 查询漏过。
- 未创建 ToolRun、ToolResult、Asset、Registry、Runtime、迁移、后台任务、队列、SSE 或 M5 内容。

## M4B 精确 allowlist

- `scripts/dev/check-scope.ps1`
- `docs/progress/phase-1-current-status.md`
- `backend/src/materialsagent/application/chat_orchestration.py`
- `backend/src/materialsagent/application/errors.py`
- `backend/src/materialsagent/domain/ports/chat_orchestration.py`
- `backend/src/materialsagent/domain/ports/unit_of_work.py`
- `backend/src/materialsagent/infrastructure/llm/mock.py`
- `backend/src/materialsagent/infrastructure/db/conversation_task.py`
- `backend/src/materialsagent/api/dependencies.py`
- `backend/src/materialsagent/api/routes/conversations.py`
- `backend/src/materialsagent/main.py`
- `backend/tests/unit/test_chat_orchestration_service.py`
- `backend/tests/contract/test_chat_orchestration.py`
- `backend/tests/integration/db/test_chat_orchestration_persistence.py`
- `backend/tests/api/test_conversations.py`
- `backend/tests/api/test_message_orchestration.py`
- `backend/tests/api/test_tasks.py`

`domain/ports/chat_orchestration.py` 原则上属于已验收的 M4-A 文件。本轮先前只增加 Service 已依赖的 provider/model_name metadata 协议；本次复审修正进一步把 timeout/provider/protocol 三类安全异常移入正式 Port 失败契约，并删除错误的 Adapter 级 `provider_request_id` 静态声明。未修改三类 route、候选结构或业务语义。

## 五类正式映射

1. KNOWLEDGE_ANSWER：LLMCall `SUCCEEDED`；Task `KNOWLEDGE_QA/SUCCEEDED`；一条 LLM AssistantMessage；无 Revision；HTTP 200。
2. NEEDS_INPUT/正式缺失或歧义：LLMCall `SUCCEEDED`；revision 1 保存正式重算字段；Task `TOOL_EXECUTION/NEEDS_INPUT`；受控追问；HTTP 200。
3. 完整但硬校验非法：LLMCall `SUCCEEDED`；revision 1 保存有界 validation details；Task `TOOL_EXECUTION/FAILED`；HTTP 422。
4. 完整合法 ToolCandidate：LLMCall `SUCCEEDED`；revision 1；Task `TOOL_EXECUTION/FAILED`、`TOOL_UNAVAILABLE`；无 AssistantMessage/ToolRun/Result/Asset；HTTP 503。
5. 编排失败：timeout/provider/protocol 分别安全映射；LLMCall 和 Task 均 `FAILED`；不创建伪造 Message/Revision；timeout 为 504 `UPSTREAM_TIMEOUT`，provider 为 503、protocol 为 502，后两者公共 code 为 `CHAT_ORCHESTRATION_FAILED`。

## 最终验证

- 定向聚焦：`96 passed in 9.06s`，覆盖 Application unit、Chat contract、PostgreSQL integration 和 API。
- Chat contract：`34 passed in 0.06s`。
- PostgreSQL 集成累计 13 项，其中新增终态组合、Revision 数量及跨 Task 错绑真实数据库测试 3 项。
- 完整 Backend：`398 passed in 30.47s`，0 failed，0 skipped；高于 380 复审前基线且未删除、跳过或弱化现有测试。
- `python -m pip check`：`No broken requirements found.`
- Alembic heads/current：均为 `0004_llm_call (head)`；check 为 `No new upgrade operations detected.`
- `check-sem-integrity.ps1`：`SEM_INTEGRITY_OK`，57 files，`total_size_bytes=2043071133`，fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- `check-scope.ps1 -Milestone M4B`：`SCOPE_OK M4B`。
- 主开发数据库：actor=1，conversation=0，message=0，task=0，task_input_revision=0，llm_call=0；临时数据库=0。
- 最终环境：PostgreSQL 与 MinIO 均为 `Exited (0)`；`materialsagent_postgresql_data`、`materialsagent_minio_data` 两个 named volume 均保留。
- `git diff --check`：退出码 0；暂存区为空。
- M4-B 已通过项目负责人最终代码验收；上述验证证据随本次唯一验收提交固化。

## 真实 HTTP 人工验收

使用一次性 `materialsagent_acceptance_*` 数据库迁移到 0004，启动真实 Uvicorn 应用工厂，通过 loopback HTTP 重新执行复审要求的四条路径，并逐项从 PostgreSQL 回查：

| 场景 | HTTP | Task | Assistant | Revision | LLMCall | 关键字段 |
|---|---:|---|---|---|---|---|
| 知识回答 | 200 | KNOWLEDGE_QA/SUCCEEDED | 有 | 无 | SUCCEEDED | selected IDs 均空 |
| 缺 aging_temperature | 200 | TOOL_EXECUTION/NEEDS_INPUT | 有 | revision 1 | SUCCEEDED | missing=[aging_temperature] |
| 完整合法 Tool | 503 | TOOL_EXECUTION/FAILED | 无 | revision 1 | SUCCEEDED | TOOL_UNAVAILABLE；selected IDs 均空 |
| Mock timeout | 504 | null/FAILED | 无 | 无 | FAILED/UPSTREAM_TIMEOUT | selected IDs 均空 |

复验库当时计数为 actor=1、conversation=4、message=6、task=4、task_input_revision=2、llm_call=4。验收后 Backend 进程已停止，一次性数据库已删除，`REVIEW_HTTP_TEMP_DATABASES_REMAINING=0`。

## 实际修改文件

与上方 M4B 精确 allowlist 完全一致。没有 Alembic、阶段实施计划、checklist、五份设计基线、SEM、M5 或环境 Secret 变化。

## 已知风险与未实现能力

- 当前 responder 是仅用于已批准验收语句的确定性本地 Mock，不是真实 LangChain 或真实 LLM Provider。
- 完整合法候选按批准规则固定返回 `TOOL_UNAVAILABLE`；M5 Registry/Mock Runtime/ToolRun、M6 Asset/MinIO、M7 Result/Explanation/正式成功投影均未开始。
- M8 才实现公共 IdempotencyRecord 和 HTTP 重放；本轮只有内部终结安全。
- 未实现真实 Runtime、图片流程、上传、时间线、重试、Worker/队列/SSE/WebSocket、登录或前端。
- 安全关联日志为 best-effort；日志设施故障不会改变 PostgreSQL 事实或 HTTP 业务映射。

## 下一步

M4-B 验收提交完成后，下一独立工作单元可从实际 `git rev-parse HEAD` 恢复并开始 M5。当前 M5 尚未开始。
