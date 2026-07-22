# 阶段 1 当前执行状态

> 本文件只保存当前状态、最近一次工作单元摘要和下一步。完整历史由未来经项目负责人验收后的 Git commit 保存。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M5 |
| 当前工作单元 | M5 唯一验收提交 |
| 状态 | `READY_FOR_M6` |
| 上一已完成工作单元 | M4-B 业务实现，已验收并提交 |
| M4 | 已完成并保持回归语义 |
| M5 | 已通过项目负责人审查；聚焦修订与最终验证完成；Critical / Important / Minor 均为 0 |
| M6 | 尚未开始 |
| 当前 branch / HEAD | `main` / 本文件随 M5 唯一验收提交落盘，实际提交以 `git rev-parse HEAD` 为准 |
| 起始工作区 | M5 首次实现起点为 clean；本次聚焦修订起点为未提交的 M5 allowlist 工作区，暂存区为空 |
| 当前工作区 | M5 唯一验收提交完成后应为 clean；M6 未开始 |
| 已确认设计基线 | 五份设计基线均未修改 |
| 是否处于项目负责人暂停点 | 是 |
| 更新时间 | `2026-07-22T09:37:48+08:00` |

## M5 项目负责人验收结论

- M5 已通过项目负责人审查；代码审查最终结论为 Critical=0、Important=0、Minor=0。
- M5 首次实现、4 项 Important 聚焦修订及最终复审发现的 2 项契约缺口均已完成。
- 最新验证保持为审查时的 `68 passed`、原 M5 聚焦 `63 passed`、完整 Backend `464 passed`；本次唯一验收提交只更新本进度文件，不重复运行完整测试。
- Alembic head/current 为 `0005_tool_run`；M5 scope、SEM 原 fingerprint 和 Git diff 检查均通过。
- Runtime 成功后 ToolRun 暂时保持 `RUNNING` 是 M5→M7 的既定设计边界，不代表公共 Task 已成功。
- failure finalization 持久化失败时返回安全内部错误，数据库保留真实 `RUNNING` 事实，不误报 `FAILED` 已提交。
- M6 尚未开始；当前状态仅为 `READY_FOR_M6`，等待项目负责人明确确认后才能进入 M6。

## M5 已完成内容

- 建立只注册 `zta35g_sem_virtual_lab` 的静态 Tool Registry；`tool_version=0.1.0`、`schema_version=1.0` 和 MaterialTool 解析均以 Registry 为唯一事实来源。
- 建立安全 Catalog 投影与 `GET /api/v1/tools`、`GET /api/v1/tools/{tool_id}`；动态 availability 只调用 Runtime ready，不触发 execute。
- 建立独立 `mock-runtime/`：固定监听 `127.0.0.1`，live/ready/execute 均要求 `X-ZTA35G-Runtime-Token`，execute 使用非阻塞单锁实现最大并发 1、无队列和即时 `RUNTIME_BUSY`。
- Mock Runtime 不访问 PostgreSQL、MinIO 或 `SEM/`，不加载真实模型；返回确定性 Base64 `.npy`，并严格校验固定 Tool/Schema 版本和 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000`。
- 建立本地 HTTP Tool Client Adapter：只发送三个关联 ID、Registry 版本、四维正式参数、requested outputs 和平台 seed；验证响应大小、ID 回显、版本、集合、图片元数据和实际运行参数；execute 固定 `retries=False`。
- 建立 MaterialTool 与 ToolExecutionOutput Port；Adapter 不接触 Mock 实现，MaterialTool 不自行选择版本。
- 新增 ToolRun Domain、SQLAlchemy Repository、UnitOfWork Port/实现和 `0005_tool_run` 迁移；建立 Task/Revision 外键、`(task_id, attempt_no)` 唯一约束及受控状态/时间/JSONB/输出集合约束。
- 建立内部 ToolExecutionService：以已持久化且已校验的 M4 Revision/LLMCall 来源解析 Tool，生成新 ToolRun/attempt/seed，并保证历史 seed 不复用。
- 建立默认关闭的受控开发验收路径：`POST /api/v1/dev/tasks/{task_id}/tool-runs` 仅接收 `task_input_revision_id`；`GET /api/v1/dev/tool-runs/{tool_run_id}` 按 Actor 隐藏资源。
- M4 公共消息 POST 未接入 Tool 成功链；完整合法候选仍为 503 `TOOL_UNAVAILABLE`，且提交消息本身不创建 ToolRun。

## M5 代码审查聚焦修订

- ToolRun failure finalization 不再吞掉行缺失、条件更新、数据库或 commit 异常。只有等价 `FAILED` 事实已经存在或本次 `FAILED` 更新成功提交后，Application 才投影原 Runtime 错误；否则返回安全 `RESOURCE_CONFLICT`、`INTERNAL_ERROR` 或 `DEPENDENCY_UNAVAILABLE`。
- M5 开发执行只接受 `TOOL_EXECUTION/FAILED/TOOL_UNAVAILABLE` 且两个 selected 引用均为空的 M4 暂停 Task；知识 Task、非 FAILED、其他错误及已有 selected 引用的 Task 均在 Runtime 调用前拒绝。合法同一 Task 的第二次显式开发执行仍创建新 ToolRun、attempt 和 seed。
- Local ZTA35G Adapter 对 `SUCCEEDED/PARTIALLY_SUCCEEDED` 强制一张 SEM，并按三种请求模式校验 `generated_sem/intermediate_sem`、`requested_output` 和性能 data；性能值必须是有限非 bool 数值，单位固定为 `MPa` 与 `%`，性能未完成时 data 必须为空；双输出请求中不可能的“仅性能完成、SEM 失败”组合无论携带何种图片角色都作为协议错误拒绝。
- Runtime error 只接受第四节 B 的静态错误码集合；`safe_message` 非空、可打印且最多 256 字符，`retryable` 必须为 bool，复杂 details 只允许小型扁平安全标量。未知/非法错误统一成为 `RUNTIME_PROTOCOL_ERROR`，Backend 使用固定消息而不透传 Runtime 文本。
- Mock Runtime 的所有结构化错误也限制在同一静态 allowlist 内；协议版本不匹配与鉴权失败分别使用 `SCHEMA_VERSION_MISMATCH`、`INVALID_RUNTIME_REQUEST`。execute 未预期异常返回固定 HTTP 500 `INTERNAL_RUNTIME_ERROR`，不返回异常 repr、路径或 traceback；执行锁由 `finally` 释放，下一请求可成功；响应上限按紧凑 JSON 实际 UTF-8 bytes 判断。

## ToolRun 生命周期与事务边界

1. 短事务校验 owned Task、Revision、来源 LLMCall 和 Registry，创建 `PENDING` ToolRun 并提交。
2. 新短事务以条件更新把该 ToolRun 改为 `RUNNING` 并提交。
3. 关闭所有 UnitOfWork/Session 后，在事务外通过 MaterialTool→HTTP Adapter 调用 Runtime 一次。
4. Runtime/Adapter 失败使用新短事务把 ToolRun 终结为 `FAILED`，记录固定安全错误和 failed outputs；只有该事实已提交才投影 Runtime 错误，终结持久化失败时不谎报 `FAILED`。
5. M5 Runtime 成功只安全保存 actual runtime parameters、diagnostics 和不含 Base64/bytes 的 output summary；ToolRun 保持 `RUNNING`，顶层 completed/failed 仍为空，等待 M7 的 Asset/ToolResult 完整聚合。
6. M4 Task 始终保持 `TOOL_EXECUTION/FAILED`、`TOOL_UNAVAILABLE`，selected references 均为空；M5 不伪造公共成功或部分成功。

## Runtime 错误映射与调用次数

| Runtime/Adapter 情况 | 公共开发验收映射 | ToolRun | 单次显式执行的 Adapter 调用数 |
|---|---|---|---:|
| timeout | 504 `RUNTIME_TIMEOUT` | FAILED | 1 |
| 未启动/连接拒绝 | 503 `RUNTIME_UNAVAILABLE` | FAILED | 1 |
| busy | 503 `RUNTIME_BUSY`，retryable=true | FAILED | 1 |
| 协议/回显/版本/大小错误 | 502 `RUNTIME_PROTOCOL_ERROR` | FAILED | 1 |
| failure commit 失败 | 500 `INTERNAL_ERROR` | 保持 RUNNING | 1 |
| failure 条件更新冲突且事实不等价 | 409 `RESOURCE_CONFLICT` | 读取到的实际状态 | 1 |
| Mock success | 201，安全 ToolRun 投影 | RUNNING | 1 |

`urllib3.exceptions.NewConnectionError` 同时继承 timeout 异常；本轮用契约回归明确把它优先映射为 unavailable，真正 timeout 仍映射为 timeout。Backend 不自动重试 execute。

## 最终自动化验证

- 本次审查问题聚焦命令：`68 passed in 3.32s`，0 failed。
- 计划规定的原 M5 聚焦命令：`63 passed in 6.09s`，0 failed。
- 完整 Backend：`464 passed in 34.07s`，0 failed，0 skipped；未删除、跳过或弱化既有测试。
- Mock Runtime：11 项；Runtime/Adapter contract：36 项；全部 Backend contract：70 项；ToolExecutionService unit：15 项；ToolRun PostgreSQL：3 项；tools API：6 项；迁移：5 项。
- M4 API 与 PostgreSQL 定向回归：20 项通过；完整合法消息仍为 503，消息提交后 ToolRun 行数为 0，Asset/ToolResult 不存在，selected references 为空。
- failure commit 注入的真实 tools HTTP/PostgreSQL 测试返回 500 `INTERNAL_ERROR`；Adapter 只调用 1 次，对应 ToolRun 保持 `RUNNING`、`error_code=null`，未误报 `RUNTIME_TIMEOUT` 已持久化。
- `python -m pip check`：`No broken requirements found.`
- `python -m compileall -q backend/src mock-runtime/src`：退出码 0。
- Alembic `heads` / `current`：均为 `0005_tool_run (head)`；`check`：`No new upgrade operations detected.`
- `check-scope.ps1 -Milestone M5`：`SCOPE_OK M5`。
- `check-sem-integrity.ps1`：`SEM_INTEGRITY_OK`，57 files，`total_size_bytes=2043071133`，fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- `git diff --check`：退出码 0；暂存区为空。
- PostgreSQL/MinIO 验证后已执行 `docker compose down`，未使用 `-v`；`materialsagent_postgresql_data` 与 `materialsagent_minio_data` 均保留。

## 进程级人工验收证据

使用一次性测试数据库迁移到 `0005_tool_run`，隐藏启动独立 Mock Runtime，再由真实 Backend HTTP Adapter 执行：

| 验收项 | 实际结果 |
|---|---|
| 正确 Token ready | 200，`READY` |
| 错误 Token live / ready / execute | 401 / 401 / 401 |
| Catalog availability | `AVAILABLE` |
| 正常消息完整合法 Tool 请求 | 503 `TOOL_UNAVAILABLE`；开发执行前 ToolRun=0 |
| 受控开发成功执行 / 查询 | POST 201；GET 200；ToolRun `RUNNING` |
| 未启动 Runtime | 503 `RUNTIME_UNAVAILABLE`；对应 ToolRun `FAILED` |
| 两次显式执行 | attempts=[1,2]；tool_run_id 不同；seed 不同 |
| M4 Task | `FAILED/TOOL_UNAVAILABLE`；selected references=[null,null] |
| Asset / ToolResult | 两张表均不存在 |

验收后 Runtime 进程已停止，一次性数据库已删除；未保留 Token、Base64、图片 bytes 或临时验收文件。

## M5 精确 allowlist

- `.env.example`
- `mock-runtime/pyproject.toml`
- `mock-runtime/src/materialsagent_mock_runtime/main.py`
- `mock-runtime/tests/conftest.py`
- `mock-runtime/tests/test_runtime.py`
- `backend/alembic/env.py`
- `backend/alembic/versions/0005_create_tool_run.py`
- `backend/src/materialsagent/domain/models/tool_run.py`
- `backend/src/materialsagent/domain/ports/tool_execution.py`
- `backend/src/materialsagent/domain/ports/unit_of_work.py`
- `backend/src/materialsagent/application/tools.py`
- `backend/src/materialsagent/application/tool_execution.py`
- `backend/src/materialsagent/infrastructure/config.py`
- `backend/src/materialsagent/infrastructure/db/conversation_task.py`
- `backend/src/materialsagent/infrastructure/db/tool_run.py`
- `backend/src/materialsagent/infrastructure/db/unit_of_work.py`
- `backend/src/materialsagent/infrastructure/tool_clients/local_zta35g.py`
- `backend/src/materialsagent/api/dependencies.py`
- `backend/src/materialsagent/api/routes/tools.py`
- `backend/src/materialsagent/main.py`
- `backend/tests/unit/test_config.py`
- `backend/tests/unit/test_tool_execution_service.py`
- `backend/tests/contract/test_runtime_contract.py`
- `backend/tests/integration/db/test_tool_run.py`
- `backend/tests/integration/db/test_migrations.py`
- `backend/tests/integration/db/test_chat_orchestration_persistence.py`
- `backend/tests/api/test_tools.py`
- `backend/tests/api/test_message_orchestration.py`
- `scripts/dev/check-scope.ps1`
- `docs/progress/phase-1-current-status.md`

`backend/tests/integration/db/test_chat_orchestration_persistence.py` 是实施中发现的必要 M4 回归更新：M5 后正确事实从“ToolRun 表不存在”变为“ToolRun 表存在但公共消息提交产生 0 行”。该文件只修改这一条断言，未修改 M4 实现或业务映射。

## 已知风险与未实现能力

- 当前 Runtime 是确定性 Mock，不是真实 ZTA35G 推理；真实环境、权重和兼容性验证属于 M13–M16。
- M5 成功 ToolRun 有安全待提交输出但仍为 `RUNNING`；正式 `.npy` 解码、PNG、MinIO Asset 属于 M6，ToolResult/Explanation/selected references/稳定 Task 成功聚合属于 M7。
- 开发验收路由默认关闭，只能显式配置启用；公共 Tool retry 属于 M8，当前没有公共重试端点。
- Backend 不负责自动启动、重启或守护 Runtime。
- 未实现 Asset、ToolResult、Explanation、真实 LLM、Redis、Worker、队列、SSE、WebSocket、登录、上传、多 Tool、前端完整功能；未修改或运行 `SEM/`。

## 下一步

完成 M5 唯一验收提交后停止；不 push、不 amend、不开始 M6，等待项目负责人明确确认进入 M6。
