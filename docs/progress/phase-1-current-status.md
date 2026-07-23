# 阶段 1 当前执行状态

> 本文件只保存当前动态状态、最近工作单元证据和下一步。产品与架构语义仍以五份已确认设计基线和阶段 1 实施计划为准。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M7 |
| 当前工作单元 | M7-A Result、M7-B Explanation、M7-C 公共同步 Tool 链 |
| 状态 | `READY_FOR_M7_ACCEPTANCE_COMMIT` |
| 上一已验收工作单元 | M6 唯一验收提交 `feat: add generated asset lifecycle` |
| M6 | 已验收 |
| M7 | 项目负责人代码审查已批准；唯一验收提交待创建 |
| M7 project-owner review | `APPROVED` |
| M7 acceptance commit | `PENDING` |
| M8 | `NOT STARTED` |
| 实际 branch / HEAD | `main` / `19d540dc490f42d0e6c6e3695e558ed49f5e26d3` |
| HEAD parent / subject | `8d75ad4974711880452b1c32384e6f34301bb3d7` / `feat: add generated asset lifecycle` |
| 暂存区 | 空；未执行 `git add` |
| 当前工作区 | M7 allowlist 内 27 个 tracked 修改和 22 个 untracked 新文件 |
| 已确认设计基线 | 五份均未修改 |
| 历史 migration | `0001`–`0006` 均未修改 |
| `SEM/` | 未修改、未运行真实模型；完整性检查通过 |
| Mock Runtime | 实现和协议未修改 |
| Git 外部动作 | 未 commit、未 push、未 amend |
| 是否处于项目负责人暂停点 | 否；已获准创建唯一 M7 验收提交 |
| 更新时间 | `2026-07-23T16:42:29+08:00` |

## M7 已实现内容

- 新增不可变 `ToolResult`、`ResultAssetLink` 和 `NaturalLanguageExplanation` Domain；输出集合、状态、结构化数据、provenance、错误和安全 JSON 均受控。
- 新增唯一 migration `0007_tool_result_explanation`，`down_revision = 0006_asset`；建立 Result、Link、Explanation、Task selected result 和 LLM input result 的约束与索引。
- 新增 Repository、SQLAlchemy 实现和 UoW 接线；Result 提交在一个短事务内锁定并重验 Task、ToolRun、Asset、Receipt 来源，原子写入 Result/Link、终结 ToolRun，并更新 Task selected references。
- Result 只关联同 Actor、Task、ToolRun 的 `AVAILABLE` Asset。SEM 成功精确要求一个 requested-output Asset；只请求性能且失败时允许 intermediate Asset，但不会把 Task 提升为部分成功。
- Explanation 采用 `prepare → 事务外 Mock 调用 → finalize`。prepare 先提交 PENDING LLMCall/Explanation；finalize 在短事务中重验 Actor/Conversation/Task/ToolRun/Result/Asset/LLMCall/Explanation 来源并聚合 Task。
- Explanation 条件更新返回 `None` 时先回滚当前 UoW，再用全新 UoW 读取并验证完整等价终态；不会把当前 Session 的未提交对象当作成功事实。
- `provider_request_id`、Explanation text、safe error 和数据库约束都有长度与形状边界；不保存 Prompt、provider raw response、Secret 或不受控元数据。
- 公共消息 POST 在完整 M7 依赖装配时同步执行 Chat → Runtime → Asset → Result → Explanation；响应来自最后一次新 UoW 数据库重查，不创建 Tool AssistantMessage。
- 结果链未完整装配或显式关闭时保持原 503 `TOOL_UNAVAILABLE`，Runtime、ToolRun、Asset、Result、Explanation 均为 0。
- 新增 `GET /api/v1/tool-results/{result_id}`；不存在与无权访问统一 404。公共 Result/Task/消息投影只返回安全摘要和 Asset content URL。
- Runtime 受控业务失败可形成正式 FAILED Result；Runtime 传输失败保留 FAILED ToolRun 并让 Task 选择该 ToolRun，不伪造 Result。
- Asset 失败按实际 UTC 时间终结 Task/ToolRun；Result 或 Explanation 持久化失败不返回内存成功事实。
- Result 原子提交失败后，Workflow 使用新短事务锁定并重查 Task、ToolRun 和该 ToolRun 的 Result；确认 Result 不存在且来源事实仍为未选择的 RUNNING 后，将 Task/ToolRun 稳定终结为 `FAILED / RESULT_PERSISTENCE_FAILED`，保留 AVAILABLE Asset、diagnostics 和 output_summary，再重新抛出原持久化错误。
- 如果 Result 实际已经提交，则失败终结不会覆盖真实 Result/Link/selected references；如果失败终结事务也提交失败，则不声称 FAILED 已落库，实际 Task/ToolRun 仍保持数据库中的 RUNNING。
- `0007` downgrade 在删除 M7 表前删除 Explanation 和 `TOOL_RESULT_EXPLANATION` LLMCall、清空 Task 的 M7 selected Result 引用并确认没有剩余 LLM input Result 引用；M7 数据按 migration 语义删除，M6 ToolRun/Asset 历史保留。
- ToolResult 的 `data`、`warnings`、`provenance`、`error` 使用真实规范 JSON UTF-8 编码字节数执行 4096-byte 上限，不再使用手工估算。
- M7 自动装配除开关、UoW、Asset/Storage、Result 和 Explanation 边界外，还要求真实 Runtime 配置、显式 ToolExecutionService 或显式完整 ToolWorkflowService 三者之一；只有开关和存储而没有可执行 Tool 边界时保持 `TOOL_UNAVAILABLE`，不会创建 Workflow 或触发外部调用。
- ToolResult 公共 provenance 固定为 `input_revision`、`normalized_process_parameters`、`actual_runtime_parameters`。Result 原子事务读取对应 TaskInputRevision，校验同 Task、Revision ID 一致和 normalized_input 已提交，再从该 Revision 投影规范化工艺参数；不公开内部 Revision ID。
- Explanation 继续从已提交 ToolResult 的安全 provenance 读取规范化工艺参数；失败 Outcome 的 `error_code` 和 `safe_error_message` 分别限制为非空、可打印、最多 64/256 字符。
- Explanation Port 调用后的非受控普通异常、ValueError/TypeError、非法 Outcome 构造及非 ExplanationOutcome 返回均转换为固定安全失败 Outcome，并继续走原 finalize 短事务；不捕获 KeyboardInterrupt/SystemExit，不改变 finalize 持久化失败契约。
- Result 提交事务进一步校验 Revision 与 ToolRun 的 request_id、材料、两层 requested_outputs、四维参数标准单位和值一致性；不转换单位、不重排或补全输出，不信任不一致 Revision。

## 本轮负责人审查修订证据

### Explanation 非预期异常与完整 Revision 来源链

- 用户指定五文件首次红灯：`12 failed, 64 passed in 26.01s`，精确覆盖 6 个 Revision/ToolRun 来源缺口、5 个 Adapter 异常/非法返回未终结缺口，以及 1 个公共 API 500 缺口。
- 补充 TypeError 映射测试先单独红灯：错误被映射成通用 `EXPLANATION_FAILED`，而不是要求的 `EXPLANATION_PROTOCOL_ERROR`；加入最小 TypeError 分支后转绿。
- RuntimeError 映射为固定 `EXPLANATION_FAILED / Explanation generation failed.`；ValueError、TypeError、ExplanationProtocolError、None、普通 dict 和违反 Outcome 契约的构造均映射为固定 `EXPLANATION_PROTOCOL_ERROR / Explanation provider returned an invalid response.`。
- 上述失败均调用 finalize：LLMCall 与 Explanation 为 FAILED，Explanation.text 为空；已提交 ToolResult、ResultAssetLink、AVAILABLE Asset 和原 ToolRun 终态保持不变；成功 Result 对应 Task 聚合为 PARTIALLY_SUCCEEDED。持久化字段不含异常原文、异常类名或 provider payload。
- KeyboardInterrupt 和 SystemExit 专项确认不会被 Exception 边界捕获；两者继续向上抛出，当前已提交 RUNNING Explanation/LLMCall 如实保留。
- 公共 API 的普通 Adapter RuntimeError 场景返回 HTTP 200、Task PARTIALLY_SUCCEEDED、Result SUCCEEDED、AVAILABLE Asset 和 Explanation FAILED；Runtime execute 精确 1 次、Storage put 精确 1 次，响应不含 `private provider failure`、RuntimeError 或 traceback。
- ResultService 对 Revision request_id、material、Revision requested_outputs、ToolRun execution_input requested_outputs、时间单位和温度单位六类不一致均拒绝提交；ToolResult/ResultAssetLink 为 0，Task/ToolRun 保持 RUNNING，selected references 保持为空。合法来源继续由既有原子提交和公共 provenance 测试覆盖。

### Runtime 激活、公共 provenance 与 ExplanationOutcome

- 用户指定七文件首次红灯：`16 failed, 91 passed in 21.03s`。失败精确覆盖 Runtime 缺失仍装配 Workflow、旧 provenance、ResultService 未读取/校验 Revision，以及 ExplanationOutcome 接受 65/257 字符和不可打印字符。
- Runtime 配置缺失、未注入 ToolExecutionService/ToolWorkflowService、M7 开关打开且 UoW/Storage 可用的 API 场景，返回 503 `TOOL_UNAVAILABLE`；应用没有装配 ToolWorkflow，Runtime execute 为 0，Storage put/HEAD/get/delete 均为 0。
- 同一场景数据库中 ToolRun、Asset、ToolResult、NaturalLanguageExplanation 和 `TOOL_RESULT_EXPLANATION` LLMCall 均为 0；Task 为 FAILED/`TOOL_UNAVAILABLE`，`selected_tool_run_id` 与 `selected_result_id` 均为空。
- 正常 Result 的实际公共 provenance 为 revision 序号 `input_revision=1`、来自已提交 normalized_input 的四项带单位工艺参数，以及受控 `actual_runtime_parameters`；消息 POST 与 ToolResult GET 返回相同 JSON，响应文本不含 `task_input_revision_id`。
- Result 提交事务的故障注入 Repository 确认查询 ID 为 `revision_1`；返回跨 Task Revision、返回 ID 不一致 Revision、或返回 `normalized_input=None` 时均抛出冲突且不写 Result，不改变 RUNNING Task/ToolRun 与 selected references。
- Explanation prepare 的 `process_parameters` 与已提交 ToolResult 的 `normalized_process_parameters` 完全一致。失败 Outcome 恰好 64 字符 error code 和 256 字符 safe message 被接受；65/257 字符及包含换行的不可打印值均被拒绝。既有 timeout/provider/protocol 固定安全映射未改变。

### 前一轮 Result persistence、migration round-trip 与 JSON 字节边界

- 首次四文件聚焦红灯：`6 failed, 58 passed`；其中 5 个是预期产品缺口，另 1 个是新增中文 JSON 测试的外层编码开销断言少算 1 byte。
- 修正测试算术后再次确认有效红灯：`5 failed, 59 passed`，分别覆盖 Result 失败终结、0007 M7 引用清理、双引号/反斜杠转义和精确 4096-byte 边界。
- Result commit 第一次失败、后续失败终结成功：ToolResult/ResultAssetLink/Explanation/Explanation LLMCall 均为 0；Task 和 ToolRun 为 FAILED，错误码均为 `RESULT_PERSISTENCE_FAILED`；ToolRun completed_outputs 为空、failed_outputs 为全部 requested_outputs；Task 选择当前 ToolRun 且 selected_result_id 为空；AVAILABLE Asset、diagnostics、output_summary 保留。
- 同一故障路径的公共 API 返回安全 500 `RESULT_PERSISTENCE_FAILED`，不含内存 Result；Runtime execute 精确 1 次，MinIO put 精确 1 次，没有因 Result 或失败终结而增加调用。
- 失败终结事务也失败：仍返回 `ResultPersistenceError`；数据库中的 Result/Link/Explanation 仍为 0、Asset 仍 AVAILABLE，Task/ToolRun 保持实际 RUNNING 且没有错误码或 selected references。
- 不确定提交测试确认：如果 Result/Link 和 selected references 已真实提交，则不会覆盖为失败；保留 Result 和 ToolRun SUCCEEDED/Task RUNNING 的等待 Explanation 事实，同时仍向调用方报告安全持久化错误。
- migration 测试在 0007 写入 Task→ToolRun→AVAILABLE Asset→ToolResult→ResultAssetLink→`TOOL_RESULT_EXPLANATION` LLMCall→Explanation→selected references 完整链，downgrade 到 0006 后清除 M7 引用，再 upgrade 到 0007 成功；原 ToolRun 和 AVAILABLE Asset 各保留 1 行，M7 Result/Explanation 不保留。
- JSON 测试覆盖大量双引号、大量反斜杠、多字节中文、真实编码恰好 4096 bytes、4097 bytes，以及 data/warnings/provenance/error 四个字段的大小保护。

## 事务与外部调用边界

1. Chat prepare/finalize、ToolRun prepare/start/finalize、Asset Tx1/Tx2、Result commit、Explanation prepare/finalize 都是独立短事务。
2. Runtime execute 在数据库 UoW 外，单次用户提交精确调用一次，无自动重试。
3. 图片解码、PNG 编码和 MinIO put/HEAD 在数据库 UoW 外；每张图片只执行一次 put。
4. Explanation Mock Adapter 在数据库 UoW 外；集成测试用活动 UoW 计数器明确断言调用时为 0。
5. 公共成功响应只在 Result 与 Explanation 已提交后，通过新 UoW 重查 Task、ToolRun、Result、Link、Asset、Explanation 和 LLMCall 后组装。

## 自动化验证

- 本轮用户指定五文件聚焦集：`79 passed in 23.23s`。
- 本轮用户指定七文件聚焦集：`107 passed in 21.37s`。
- 用户指定四文件聚焦集：`64 passed in 11.18s`。
- 公共 Result persistence failure API 专项：`1 passed in 0.92s`。
- 完整 Backend：`704 passed in 65.58s`，0 failed、0 skipped。
- 独立 Mock Runtime：`11 passed in 1.07s`。
- Backend 环境 `pip check`：`No broken requirements found.`。
- `python -m compileall -q backend/src mock-runtime/src`：exit 0。
- Alembic `heads` / `current`：`0007_tool_result_explanation (head)`；`check`：`No new upgrade operations detected.`
- `check-scope.ps1 -Milestone M7`：`SCOPE_OK M7`。
- `check-sem-integrity.ps1`：`SEM_INTEGRITY_OK`，57 files，`total_size_bytes=2043071133`，fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- `git diff --check` 通过；提交前暂存区保持为空。

## 真实 HTTP / PostgreSQL / MinIO / Mock Runtime 人工验收

使用随机一次性验收数据库、仓库 Mock Runtime HTTP 服务、Backend HTTP 服务和真实 MinIO；四个开放场景各自 Runtime execute = 1、Storage put = 1，关闭场景均为 0。

| 场景 | HTTP | Task / ToolRun / ToolResult | Asset | Explanation / LLMCall | 关键核对 |
|---|---:|---|---|---|---|
| A 全部成功 | 200 | SUCCEEDED / SUCCEEDED / SUCCEEDED | AVAILABLE | SUCCEEDED / SUCCEEDED | 性能值 650.0 来自 Mock Runtime；Result API 200；inline/attachment 200；无 AssistantMessage |
| B 图片成功、性能失败 | 200 | PARTIALLY_SUCCEEDED / PARTIALLY_SUCCEEDED / PARTIALLY_SUCCEEDED | AVAILABLE | SUCCEEDED / SUCCEEDED | completed 仅 sem_image；failed 仅 mechanical_properties；data 无性能值 |
| C 只请求性能且失败 | 200 | FAILED / FAILED / FAILED | AVAILABLE intermediate | SUCCEEDED / SUCCEEDED | completed 为空；failed 为 mechanical_properties；intermediate 未提升 Task |
| D Explanation timeout | 200 | PARTIALLY_SUCCEEDED / SUCCEEDED / SUCCEEDED | AVAILABLE | FAILED / FAILED | Result、Link、Asset、ToolRun 保留；Explanation 没有额外 Storage put |
| E 结果链关闭 | 503 `TOOL_UNAVAILABLE` | ToolRun 0 / Result 0 | 0 | 0 | Runtime execute 0；Storage put 0 |

数据库逐场景核对了 Actor、Conversation、Task、ToolRun、Result、Asset、Link、Explanation、LLMCall 的来源 ID、selected references、Tool/Schema 版本、输出集合和终态时间。公共响应不含 object key、bucket、Runtime/MinIO 定位、model bundle、Base64、`.npy`、Prompt、provider raw response、traceback 或 Secret。

验收结束后已停止三个临时 HTTP 进程，删除本轮四个 MinIO 对象并丢弃一次性数据库；复核 `acceptance_databases_remaining=0`。随后执行 `docker compose down`，未使用 `-v`；`materialsagent_postgresql_data` 与 `materialsagent_minio_data` 命名卷仍保留。

## 实际修改文件

### Migration、Domain、Repository 与 Application

- `backend/alembic/env.py`
- `backend/alembic/versions/0007_create_tool_result_explanation.py`
- `backend/src/materialsagent/domain/models/explanation.py`
- `backend/src/materialsagent/domain/models/llm_call.py`
- `backend/src/materialsagent/domain/models/result_asset_link.py`
- `backend/src/materialsagent/domain/models/tool_result.py`
- `backend/src/materialsagent/domain/models/tool_run.py`
- `backend/src/materialsagent/domain/ports/explanation.py`
- `backend/src/materialsagent/domain/ports/unit_of_work.py`
- `backend/src/materialsagent/infrastructure/db/asset.py`
- `backend/src/materialsagent/infrastructure/db/conversation_task.py`
- `backend/src/materialsagent/infrastructure/db/explanation.py`
- `backend/src/materialsagent/infrastructure/db/llm_call.py`
- `backend/src/materialsagent/infrastructure/db/tool_result.py`
- `backend/src/materialsagent/infrastructure/db/tool_run.py`
- `backend/src/materialsagent/infrastructure/db/unit_of_work.py`
- `backend/src/materialsagent/infrastructure/llm/mock_explanation.py`
- `backend/src/materialsagent/application/chat_orchestration.py`
- `backend/src/materialsagent/application/explanation_service.py`
- `backend/src/materialsagent/application/result_service.py`
- `backend/src/materialsagent/application/tasks.py`
- `backend/src/materialsagent/application/tool_execution.py`
- `backend/src/materialsagent/application/tool_workflow.py`

### API 与装配

- `backend/src/materialsagent/api/dependencies.py`
- `backend/src/materialsagent/api/routes/conversations.py`
- `backend/src/materialsagent/api/routes/tasks.py`
- `backend/src/materialsagent/api/routes/tool_results.py`
- `backend/src/materialsagent/main.py`

### 测试、范围与动态状态

- `backend/tests/api/conftest.py`
- `backend/tests/api/test_assets.py`
- `backend/tests/api/test_explanation_outcomes.py`
- `backend/tests/api/test_message_orchestration.py`
- `backend/tests/api/test_tasks.py`
- `backend/tests/api/test_tool_results.py`
- `backend/tests/contract/test_explanation.py`
- `backend/tests/contract/test_tool_execution_output.py`
- `backend/tests/integration/db/test_chat_orchestration_persistence.py`
- `backend/tests/integration/db/test_explanation_persistence.py`
- `backend/tests/integration/db/test_migrations.py`
- `backend/tests/integration/db/test_result_commit.py`
- `backend/tests/unit/test_chat_orchestration_service.py`
- `backend/tests/unit/test_explanation_domain.py`
- `backend/tests/unit/test_explanation_service.py`
- `backend/tests/unit/test_llm_call_domain.py`
- `backend/tests/unit/test_result_public_projection.py`
- `backend/tests/unit/test_tool_execution_service.py`
- `backend/tests/unit/test_tool_result_domain.py`
- `scripts/dev/check-scope.ps1`
- `docs/progress/phase-1-current-status.md`

以上路径全部属于 M7 allowlist；没有 allowlist 外变更。

## 已知风险

- 本轮 Explanation Adapter 是确定性 Mock，不是真实外部 LLM；这是阶段 1A 的既定边界。
- PostgreSQL 与 MinIO 仍不能形成单一 ACID 事务；M6 已有的 PENDING/ORPHANED 补偿与恢复事实继续适用。
- 本轮真实 HTTP 验收使用 Mock Runtime 的受控场景输出，不加载或运行真实 ZTA35G 模型。
- 当前变更已经项目负责人代码审查批准；唯一 M7 验收提交创建前仍不能视为已验收，也不能开始 M8。

## 下一步

仅创建已获批准的唯一 M7 验收提交；不 push、不 amend，不开始 M8。

```text
M7 project-owner review: APPROVED
M7 acceptance commit: PENDING
M8: NOT STARTED
```
