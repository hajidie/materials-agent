# 阶段 1 当前执行状态

> 本文件只保存当前状态、最近一次工作单元摘要和下一步。完整历史由未来经项目负责人验收后的 Git commit 保存。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M3：Conversation、Message、Task 最小闭环 |
| 当前工作单元 | M3-B Application Service、公共 API 与 M3 完整验收 |
| 状态 | `READY_FOR_M3_B` |
| 上一已完成工作单元 | M3-A |
| 当前 branch | `main` |
| M3-A 起点 HEAD | `eafec77f67740e62be1b45dfcb6da19577127d9e` |
| 已确认设计基线 | 第一节、第二节、第三节、第四节 A、第四节 B，共五份；本轮未修改 |
| 阶段 1 实施计划 | `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`；本轮未修改 |
| 是否处于项目负责人暂停点 | 否 |
| 更新时间 | `2026-07-19T12:55:43+08:00` |

## 最近完成：M3-A

- 验收状态：M3-A 已通过项目负责人代码与运行验收。已完成 Conversation、Message、Task、TaskInputRevision Domain；`0002` 四表 migration；Conversation/Task/Message/Revision Repository；UnitOfWork 扩展；Actor 所有权过滤；原子 rollback；唯一冲突保护；以及 `0003` Task 时间顺序约束。M3 尚未完成，M3-B 尚未开始。
- 不可变 migration 决策：`0002_create_conversation_message_task_revision` 已应用到保留主库，因此未修改、替换或删除该历史 migration；本轮前后 SHA-256 均为 `9523B0116A2551084184E6EFA41EDCDEEC92304759FA10A553622A27A5C1E5B5`，通过新 migration 演进。
- Alembic：新增 `0003_task_time_order`，down revision 为 `0002_create_conversation_message_task_revision`；唯一 head/current 均为 `0003_task_time_order`，`alembic check` 返回 `No new upgrade operations detected.`。一次性数据库已验证 `0003→0002→0003` 时四张 M3 表保留且两条新 CHECK 按预期移除/恢复，也继续通过完整 `0003→0001→0003` 往返；主库只执行 `upgrade head`，未执行 downgrade。
- 主数据库：现有 `actor` 和 `alembic_version` 保留；新增且仅新增 `conversation`、`task`、`message`、`task_input_revision` 四张业务表。主数据库四张新表的业务行均为 0，本轮未写 Conversation、Task、Message 或 Revision 人工验收数据。
- Conversation：字段为 `conversation_id/actor_id/title/created_at/updated_at`；约束非空白 ID、可空但非空白 title、UTC 时区列、`updated_at >= created_at`，并以 RESTRICT 外键指向 Actor。
- Task：字段为 `task_id/conversation_id/actor_id/task_type/current_status/selected_tool_run_id/selected_result_id/created_at/started_at/updated_at/completed_at/error_code/safe_error_message`；状态与类型使用稳定 CHECK，selected result 必须同时有 selected run，RESTRICT Actor/Conversation 外键已建立。时间顺序由既有 `completed_at >= created_at` 与 `0003` 新增的 `started_at >= created_at`、两者均存在时 `completed_at >= started_at` 三层 CHECK 共同保护。
- Message：字段为 `message_id/conversation_id/task_id/actor_id/request_id/role/generation_source/content_text/structured_content/llm_call_id/created_at`；role/source、USER/LLM/TEMPLATE 组合、非空白文本、可空 JSONB 和 LLMCall 唯一保护均有稳定约束；Actor/Conversation/Task 外键均为 RESTRICT。
- TaskInputRevision：字段为 `task_input_revision_id/task_id/request_id/source_llm_call_id/source_message_ids/revision/raw_input/normalized_input/missing_fields/ambiguous_fields/validation_errors/created_at`；`revision > 0`、`(task_id, revision)` 唯一、非空来源数组、JSONB object/array 形状和 Task RESTRICT 外键已建立。
- `source_message_ids` 选择 PostgreSQL `text[]`：ORM 映射直接、顺序天然保留、`cardinality(...) > 0` 可做简单稳定的数据库约束；Domain 另行逐项拒绝空白 opaque ID。没有建立 TaskInputRevisionMessage 表。
- 未来外键：`Task.selected_tool_run_id/selected_result_id` 的目标表分别留待 M5/M7；`Message.llm_call_id` 与 `TaskInputRevision.source_llm_call_id` 的 LLMCall 目标表留待 M4。因此本轮只建立可空 text 列和可成立的行内约束，不创建占位表或指向不存在表的外键。
- Domain/ORM 隔离：四个 Domain 模型为纯 Python dataclass，不导入 SQLAlchemy、Alembic、FastAPI、Pydantic、PostgreSQL driver 或 MinIO；ID 保持非空 opaque text，时间要求 timezone-aware UTC。Task Domain 新增 `started_at` 不早于 `created_at`、`completed_at` 不早于 `started_at` 校验及稳定 ValueError，`pending(...)` 行为未改变；TaskRow metadata 与 `0003` 的两条 CHECK 名称和表达式一致。
- Repository：新增 `conversations.get/get_owned/list_owned/add`、`messages.get/add`、`tasks.get/get_owned/add`、`task_input_revisions.get/add/list_for_task`；返回 Domain 对象，不创建 Session、不 commit、不吞异常，数据库异常继续映射到安全 PersistenceError 体系。Conversation 按 `updated_at DESC, conversation_id DESC`，Revision 按 `revision ASC, task_input_revision_id ASC`。
- UnitOfWork：保留 Actor Repository 和既有 commit/rollback/close 安全行为，新增四个 Repository 属性；同一 Session 中按 Actor→Conversation→Task→Message/Revision 顺序 flush 上游待写事实，但只由 UoW commit，异常仍原子 rollback 并关闭 Session。
- 所有权：两个 Actor 的集成测试确认 `Conversation.get_owned`、`Task.get_owned` 和 `Conversation.list_owned` 不返回其他 Actor 资源；HTTP 安全 404 语义留给 M3-B。
- 原子性与唯一保护：Conversation→Task→UserMessage 同一 UoW 的强制异常会使三者全部不落库，Session 关闭、Actor 保留且后续事务可用；重复 conversation_id/task_id/message_id 均返回安全 `PersistenceConflictError`，最终只保留一组事实。完整公共幂等留到 M8。
- 跨 Conversation 边界：数据库本轮只使用简单外键，明确不声称它单独阻止 Message.conversation_id 与 Message.task_id 所属 Conversation 的交叉组合；M3-B 必须在 Application 短事务中用 owned/get 查询校验 actor/conversation/task 来源一致性并增加自动化覆盖。
- 测试：初始 M3-A TDD 红灯为 `31 failed, 16 passed`；本次审查修复 RED 分别为 Domain `2 failed`、migration/schema `2 failed`，最小实现后聚焦测试 `4 passed`。完整 `backend/tests/integration/db` 为 `50 passed, 0 failed`；M2/M3 完整回归为 `133 collected / 133 passed / 0 failed / 0 skipped`；`pip check` 为 `No broken requirements found.`。数据库直接写入测试确认两类非法时间被各自命名 CHECK 拒绝、rollback 后数据库可用且合法时间顺序可提交。
- 运行与清理：主库只从 `0002` 执行 `upgrade head` 到 `0003`，未执行主库 downgrade/drop/reset；停服前只读确认 Actor 为 1 行、Conversation/Task/Message/Revision 均为 0 行。一次性 `materialsagent_test_*` 数据库为 0，`m2-test/` 对象为 0，配置 bucket 存在。最终 `docker compose down` 未带 `-v`；项目容器和 8000/5432/9000/9001 监听均为 0，两个既有 volume 保留，根 `.env` 仍存在、被 Git 忽略且未跟踪。
- 仓库边界：M3 精确 allowlist 已加入 `backend/alembic/versions/0003_add_task_time_order_constraints.py`；`SEM_INTEGRITY_OK`、`SCOPE_OK M3`/0，M4 仍为 `SCRIPT_CONFIGURATION_ERROR`/6。M3-A 提交范围精确为 14 个批准文件；未创建 API、Application、标题生成、HTTP 测试、M4 文件或 `__init__.py`。
- Git：M3-A 只创建一个本地验收 commit，不 push；M3-B、M4 及其他工作单元均未开始。
- M3-B 责任：在新的 Codex 对话中实现 Application 短事务来源一致性校验、Conversation 创建与列表、UserMessage/Task 原子提交、Task 查询、确定性标题、Actor 安全 404、公共 API 和人工验收；开始前必须把 M3 scope allowlist 替换为 M3-B 精确范围。

## 已知风险

- Message/Task/Conversation 的重复 actor/conversation 来源以及 Revision.source_message_ids 的同 Task/Conversation 来源尚未由复杂复合外键保证；这是已确认的 M3-B Application 短事务责任。
- 三个未来引用列在目标表创建前没有外键；后续 M4/M5/M7 migration 必须在目标表存在后增加正式关联和来源一致性验证。
- 数据库只保证 `source_message_ids` 数组非空；逐项非空由当前 Domain 保证，同 Task/Conversation 来源由 M3-B 保证。

## 下一步

保持 `READY_FOR_M3_B`。M3 尚未完成，M3-B 尚未开始；下一次新的 Codex 对话必须先重新核对 Git/current status/计划与 M3-B 精确范围，再开始 Application Service、公共 API 和 M3 完整验收。不得进入 M4。
