# 阶段 1 当前执行状态

> 本文件只保存当前动态状态、最近工作单元证据和下一步。产品与架构语义仍以五份已确认设计基线和阶段 1 实施计划为准。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M8：幂等、Tool retry、Explanation retry 与失败恢复 |
| 当前工作单元 | M8 最终核心代码复审聚焦修订 |
| 状态 | `FINAL_CORE_REVISION_COMPLETE_AWAITING_PROJECT_OWNER_REVIEW` |
| 上一已验收工作单元 | Pre-M8 行为不变止损 |
| Pre-M8 stop-loss commit | `891714dd58cf069073a7d4037be43ef66304d0ac` |
| M8 | `FINAL_CORE_REVISION_COMPLETE_AWAITING_PROJECT_OWNER_REVIEW` |
| M8 project-owner review | 最终核心复审的一个 Important 和一个 Minor 已修订；当前 `AWAITING` |
| 实际 branch / HEAD | `main` / `891714dd58cf069073a7d4037be43ef66304d0ac` |
| HEAD parent / subject | `09a9ba8ac7aea9d53f6ae2594cccc27a1f2e2b23` / `refactor: clarify pre-retry workflow boundaries` |
| 暂存区 | 空；未执行 `git add` |
| 当前工作区 | 仅下列 M8 allowlist 内修改 |
| 已确认设计基线 | 五份均未修改 |
| 历史 migration | `0001`–`0007` 均未修改 |
| `SEM/` | 未修改、未加载或运行真实模型；`SEM_INTEGRITY_OK` |
| Mock Runtime | 实现和协议未修改 |
| Git 外部动作 | 未 commit、未 push、未 amend、未 rebase、未 reset、未 stash |
| M9 | `NOT STARTED` |
| 是否处于项目负责人暂停点 | 是；下一步仅允许项目负责人代码审查 |
| 更新时间 | `2026-07-24T11:01:21+08:00` |

## M8 已实现内容

### IdempotencyRecord 与四种 operation

- 新增 migration `0008_idempotency_record`，`down_revision = 0007_tool_result_explanation`；以 `(actor_id, operation, idempotency_key)` 唯一约束裁决同 Actor、同操作、同 key 的首次请求。
- 四种 operation 精确为 `TASK_CREATE`、`TASK_INPUT_SUPPLEMENT`、`TOOL_RETRY`、`EXPLANATION_RETRY`。记录固定保存请求 digest，并按 operation 绑定 Task、Message、Revision、ToolRun 或 Explanation 的既成事实。
- Idempotency-Key 必须为 1–255 字符、非空、无 Unicode control character；key 保持不透明，不做大小写折叠或空白规范化。
- digest 使用规范 JSON：对象 key 排序、UTF-8、无多余空白、禁止 NaN，再计算 SHA-256。资源、正文、submission mode、target task、retry reason、language 等 operation 输入全部进入相应 digest。
- 同 key 同 digest 只返回当前数据库投影，`idempotency_replayed=true`，不重新调用 Chat、Runtime、MinIO 或 Explanation Provider；同 key 异 digest 固定返回 409 `IDEMPOTENCY_CONFLICT`。
- 内部幂等预留结果明确区分 `CREATED`、`RECOVERED_OWN_COMMIT` 和 `REPLAY`。只有另一 HTTP 请求命中既有记录才把公共 `idempotency_replayed` 设为 true；当前请求在 commit uncertainty 后重查到 `first_request_id` 等于自身 request_id 时继续原链路，公共 replay 保持 false。
- 短事务 commit 结果不确定时，以新 UoW 按唯一 scope 重查已提交记录及其完整资源绑定；自己的已提交预留继续后续外部调用且只调用一次，另一请求 replay 只读取数据库事实。
- M8 不实现过期、清理或 key 复用：`expires_at` 为 nullable，当前所有新记录写 `NULL`，migration 不含 expiry 顺序约束和 expires 索引。

### TASK_CREATE 与 TASK_INPUT_SUPPLEMENT

- 消息提交公共 API 强制要求 `Idempotency-Key`。首次 `TASK_CREATE` 在一个短事务内写入 Task、User Message 和 IdempotencyRecord；Chat Provider 在事务外运行。
- 同 Actor 的消息幂等预留先锁定 Actor 行，使 20 路同 key 请求在 PostgreSQL 中顺序完成唯一裁决；唯一约束仍是最终一致性防线。
- 回放投影在读取 Task 时使用 `FOR UPDATE`，避免在 Chat finalize 同时提交时跨多个 `READ COMMITTED` statement 拼接出混合快照。
- Supplement 只允许当前 Actor、同 Conversation、`NEEDS_INPUT` 且无活动运行的 Task；锁定 Task 后先拒绝该 Task 上另一条未绑定 Revision 的 Supplement record 或 PENDING/RUNNING Chat LLMCall，再写入新的 User Message 和幂等记录。不同 key 固定返回 409 `TARGET_TASK_NOT_RECOVERABLE`，Provider 在事务外调用，再写完整下一版 Revision。
- Supplement Revision 的 `source_message_ids` 覆盖该 Task 的完整用户消息链；成功补参会重新验证完整输入并只执行一次 Tool 链，回放不重复执行。

### TOOL_RETRY

- 新增 `POST /api/v1/tasks/{task_id}/tool-runs`，默认 reason 为 `USER_REQUESTED_RETRY`，成功和回放均返回稳定业务投影及 `idempotency_replayed`。
- 预留事务锁定 Task，重验 Actor、Conversation、当前 Revision、selected references、资格与无活动 attempt；按现有最大 attempt 加一，生成未使用的新 seed。
- 新 ToolRun PENDING、IdempotencyRecord 和 Task RUNNING 在同一短事务提交；Runtime execute 在 UoW 外且不自动重试。
- Runtime 成功后，Asset 生命周期仍为 PENDING → 事务外 decode/MinIO put+HEAD → AVAILABLE；Result commit 使用 retry 专用来源校验，在写 Result/Link 前按 Task 锁、新 ToolRun 锁、旧 selected ToolRun/Result 锁顺序重验旧来源、状态/输出集合、新 attempt 单调性和唯一活动 retry，再在同一短事务写 Result/Link、终结新 ToolRun并替换 Task selected references。
- 旧 ToolRun、Asset、Result 和 Explanation 历史保持可读；回放 key 不新增 Runtime execute、Storage put、ToolRun、Asset、Result 或 Explanation。
- `NEEDS_INPUT`、知识问答、硬校验失败、已成功、仅 Explanation 失败、已有活动 attempt 等不可重试状态均被拒绝；失败/部分成功及无先前 ToolRun 的受控 `TOOL_UNAVAILABLE` 失败可按完整有效 Revision 重试。

### EXPLANATION_RETRY

- 新增 `POST /api/v1/tool-results/{result_id}/explanations`；默认 language 为 `zh-CN`、reason 为 `USER_REQUESTED_RETRY`，成功和回放返回稳定业务投影及 `idempotency_replayed`。
- 预留事务按 Task → Result 顺序加锁，重验 Actor、Result、selected Result、既有 Explanation 资格；按该 Result 最大 attempt 加一。
- 新 LLMCall、PENDING Explanation、IdempotencyRecord 与 Task RUNNING 在同一短事务提交；HTTP request_id 作为新 attempt 的 request_id；Explanation Provider 在 UoW 外且只调用一次。
- finalize 复用受控安全失败映射；成功 Explanation 不改变 Result，FAILED Result 也不会因 Explanation 成功而被提升为成功 Task。
- Result 查询优先返回最新成功 Explanation；如果后续 attempt 失败，仍保留最近成功正文，并通过 `latest_explanation_failure` 暴露固定安全失败摘要；若从未成功，则返回最新终态失败。
- Tool retry 投影复用同一 Explanation attempt 选择函数，不再自行按最大 attempt 覆盖最近成功 Explanation；最近失败 attempt 仍单独表达。
- 相同 key 改变 language 或 reason 返回 409；新 key 使用不支持的 language 返回输入校验错误。

## 事务与外部调用边界

| 操作 | 锁定与写入 | commit / rollback | 外部调用 |
|---|---|---|---|
| TASK_CREATE 预留 | Actor；写 Task、User Message、IdempotencyRecord | 三者同一短事务提交；任一失败整体回滚 | Chat Provider 在提交后、UoW 外 |
| TASK_INPUT_SUPPLEMENT 预留 | Actor、目标 Task；写 User Message、IdempotencyRecord | 同一短事务提交；失败整体回滚 | Chat Provider 在提交后、UoW 外 |
| Chat finalize | Task；写 LLMCall 终态、Assistant Message/Revision、Task 聚合及 supplement record 的 Revision 绑定 | 单一短事务提交；失败回滚，回放只读既成事实 | 无 |
| TOOL_RETRY 预留 | Task；写新 ToolRun、IdempotencyRecord、Task RUNNING | 同一短事务提交；失败整体回滚 | Runtime execute 在提交后、UoW 外 |
| Asset 生命周期 | 分离的 PENDING 与 AVAILABLE/失败短事务 | DB commit 与对象存储不组成长事务；保留既有补偿语义 | decode、PNG、MinIO put/HEAD 在 UoW 外 |
| retry Result commit | Task、ToolRun、Revision、Asset 来源；写 Result/Link，终结 ToolRun，替换 Task selected references | 单一短事务原子提交；冲突或持久化失败回滚 | 无 |
| EXPLANATION_RETRY 预留 | Task、Result；写 LLMCall、Explanation、IdempotencyRecord、Task RUNNING | 同一短事务提交；失败整体回滚 | Explanation Provider 在提交后、UoW 外 |
| Explanation finalize | Task、Result、LLMCall、Explanation；写终态并聚合 Task | 单一短事务提交；冲突时回滚并以新 UoW 验证等价终态 | 无 |

Runtime、MinIO 和 Explanation Provider 调用均不在数据库 UoW 内。Backend 不自动重试 Runtime；显式 Tool retry 总是创建新 ToolRun 和新 seed。

## M8 第一轮审查修订证据

- 四类 own-reservation commit uncertainty 均以 commit 实际成功后抛 `PersistenceError` 注入：TASK_CREATE 最终 `SUCCEEDED`、Chat Provider 增量 1；SUPPLEMENT 最终 `NEEDS_INPUT`、Chat Provider 增量 1；TOOL_RETRY 最终 `SUCCEEDED`、Runtime 增量 1、Explanation 增量 1；EXPLANATION_RETRY 最终 `SUCCEEDED`、Provider 增量 1。四个原请求公共 `idempotency_replayed=false`，后续真实第二请求为 true。
- Chat `_prepare_call`、Chat `_start_call`、Tool `_start_pending_attempt`、Explanation `_start` 的实际成功后报错路径均使用新 UoW 重查。三个 start 故障注入场景的 Chat Provider、Runtime、Explanation Provider 增量分别都精确为 1，最终均为稳定 `SUCCEEDED`；没有第二次相同状态写入。
- 真实 PostgreSQL 受控并发：同一 `NEEDS_INPUT` Task、两个不同 key 和不同正文同时补充，首请求 200、第二请求 409 `TARGET_TASK_NOT_RECOVERABLE`；只新增 1 User Message、1 Supplement IdempotencyRecord、1 Chat LLMCall，Provider 增量 1，PENDING/RUNNING orphan Chat LLMCall 为 0。原 20 路同 key 用例保留并通过。
- RETRY 旧 selected references 的四种损坏注入全部在新 Result/Link 写入前被拒绝：Result 来自另一 Run、Run 来自另一 Task、result-only、旧 Run/Result 状态不一致；每种场景新 Result/Link 均为 0，旧 Task/Run/Result/Link 不变。
- `expires_at=NULL` 通过 Domain、Repository、migration schema 与 `0008 → 0007 → 0008` 往返；无 expiry check、无 expires index、无清理器或过期 key 复用。
- Tool retry 在“后续较大 attempt 为 FAILED、已有较早 SUCCEEDED Explanation”场景继续投影最近成功 Explanation；共享选择规则测试通过。

## M8 第二轮审查修订证据

### 同步 Tool 完整链的不确定提交恢复

- 初始 ToolRun PENDING 创建在 commit 报 `PersistenceError` 后，以全新 UoW 按预生成 `tool_run_id` 重读；仅当 Task、request、Revision、attempt、Tool/version/schema、requested outputs、execution input、`PENDING` 状态和 `created_at` 全部等价时继续 start 与 Runtime。测试最终只存在 1 ToolRun，Runtime、Storage put、Explanation 各调用 1 次，Task 为 `SUCCEEDED`，没有生成第二个 seed。
- Runtime success 在提交前构造完整 `recorded` RUNNING ToolRun；不确定提交后重验 attempt identity、`actual_runtime_parameters`、diagnostics、output summary、model bundle 及其余完整字段。初始链和 retry 链均继续 Asset/Result/Explanation；每条链 Runtime 只调用 1 次。
- Runtime failure 区分三种 fresh-read 结果：等价 FAILED 事实视为已提交并继续抛 `ToolExecutionOutcomeError`；仍等于提交前 RUNNING 事实视为真实持久化失败；其他混合或不一致事实为 409 冲突。完整 workflow 随后选择该失败 ToolRun，Task 最终 `FAILED`、`selected_result_id=NULL`，Runtime 只调用 1 次。
- Result 原子事务预先保留期望 Result、Links、终态 ToolRun、聚合 Task 与来源 Assets；不确定提交后按预生成 `result_id` 重读并严格核对 Result 全字段、Link 数量/asset/order、AVAILABLE 同 Run Assets、ToolRun 终态以及 Task selected references/status/error。initial 与 retry 均只保留 1 份本次 Result 并继续 Explanation attempt 1；旧“Result 已提交但 Task RUNNING、Explanation 0 次”测试已改为最终稳定 Task 与 Provider 1 次。
- 初始 Explanation prepare 在 commit uncertainty 后按预生成 `explanation_id` 与 `llm_call_id` 重读，严格核对同 Task/Result、attempt 1、ToolRun request_id、两者 PENDING、provider/model/template/digest/parameters/language 与 Explanation projection，等价时继续 start 和 Provider。
- Explanation finalize 对 initial 和 retry 均在 commit uncertainty 后调用 fresh-read 等价终态校验；完全等价返回持久化 Explanation，仍为原 RUNNING 事实保留既有 `EXPLANATION_PERSISTENCE_FAILED`，混合或不一致终态返回安全冲突。Provider 均只调用 1 次。
- Chat success finalize 在 commit uncertainty 后通过既有 projection/source identity/terminal consistency 校验，并额外核对本次知识答案或 Tool/NEEDS_INPUT 的 summary 与 Revision payload；等价时返回当前 projection。Chat failure finalize 重读并核对相同 error code/message 的 FAILED LLMCall 与 Task，使 HTTP 继续返回原持久化 outcome error；Chat Provider 不重复调用。

### selected references 全空历史损坏

- `ToolExecutionService.reserve_retry_attempt` 的 `failed_before_run` 现在同时要求 `runs == []`；已有旧 ToolRun/Result 但故障注入清空 Task 两个 selected 字段时，固定 409 `TASK_NOT_RETRYABLE`，Runtime、Storage、Explanation 与行数均不增加。
- `ResultService._validate_retry_selection` 在 selected 两项均为空时只允许 `prior_runs == []` 且当前 retry attempt 为 1；绕过 reservation 直接提交 retry Result 的 BOTH_NULL 故障测试在任何新 Result/Link 写入前返回 `ApplicationConflictError`，旧 Task/Run/Result/Link 保持不变。

## M8 最终核心复审聚焦修订证据

### Tool retry reservation 完整 selected 事实校验

- `failed_without_result` 现在显式要求 Task 已选择 FAILED ToolRun、`selected_result_id=NULL`、该 Run 属于当前 Task，且按 Run 重查确实不存在 ToolResult；`selected_result_id` 非空但按 ID 读不到 Result 不再被当成“无 Result”。
- `failed_or_partial_result` 在新 ToolRun、TOOL_RETRY IdempotencyRecord 和 Task RUNNING 写入前，逐项核对 Task selected IDs、Result Actor/Task/Run 来源、Run/Result 三组 requested/completed/failed outputs，以及 `FAILED → FAILED → FAILED` 或 `PARTIALLY_SUCCEEDED → PARTIALLY_SUCCEEDED → PARTIALLY_SUCCEEDED` 的严格 Task/Result/Run 状态链。
- 四种受控损坏——selected Result 读不到、selected Result 指向另一个 Run、selected Run/Result 状态不一致、selected Run/Result output 集合不一致——均返回 HTTP 409 `TASK_NOT_RETRYABLE`。每种场景 ToolRun 与 TOOL_RETRY IdempotencyRecord 行数不增加，Runtime/Storage put/Explanation 调用增量均为 0，Task 状态和 selected references 不变。
- `ResultService._validate_retry_selection()` 及既有五类 Result corruption 测试保持不变，继续作为 Result commit 前的第二道防线。

### Chat NEEDS_INPUT finalize 恢复等价

- NEEDS_INPUT 不确定提交恢复现在要求已持久化 AssistantMessage 存在且 `content_text` 精确等于固定 `FOLLOW_UP_TEXT`；精确正文恢复返回原 `NEEDS_INPUT` 投影，任何不同正文固定返回 409 `RESOURCE_CONFLICT`，Chat Provider 不重复调用。
- 同一分支将期望 summary 的 missing/ambiguous 集合按 LLMCall 领域模型的冻结 tuple 形式比较，避免正确持久化事实因 list/tuple 表示差异被误拒绝。

## 并发与故障恢复证据

- `20 concurrent TASK_CREATE`（知识问答）：只形成 1 Task、1 User Message、1 IdempotencyRecord 和 1 Chat Provider call；19 个响应为 replay。
- `20 concurrent TASK_CREATE`（完整 Tool）：只形成 1 Task、1 User Message、1 Revision、1 ToolRun、1 Asset、1 Result 和 1 IdempotencyRecord；Chat、Runtime、Storage put、Explanation 各 1 次。执行未完成时 replay 可返回尚无 selected references 的当前事实，所有非空 selected references 一致。
- `20 concurrent SUPPLEMENT`：只新增 1 User Message、1 Assistant Message、1 Revision、1 IdempotencyRecord 和 1 Chat Provider call；19 个响应为 replay。
- `20 concurrent TOOL_RETRY`：只新增 1 attempt、1 Runtime execute、1 Storage put、1 Result/Explanation 链；19 个响应为 replay。
- `20 concurrent EXPLANATION_RETRY`：只新增 1 Explanation attempt、1 LLMCall 和 1 Explanation Provider call；Runtime 与 Storage 调用为 0；19 个响应为 replay。
- TASK_CREATE 与 SUPPLEMENT 的两个 20 路并发用例连续重复 10 轮，每轮 `2 passed`，未再出现 PostgreSQL 唯一冲突链或混合快照 409。
- 消息预留 commit uncertainty：若重查到本 request_id 已提交的 record，分类为 `RECOVERED_OWN_COMMIT` 并继续原链路；若是另一 request_id 才分类为 `REPLAY` 并禁止外部调用。
- Tool/Explanation 预留、Chat/Tool/Explanation start、Result/Explanation finalize 的不确定提交和失败路径均由故障注入覆盖；只认可新 UoW 查到的等价既成事实。
- Runtime timeout/unavailable/protocol/业务失败与 Explanation timeout/provider/protocol/非受控普通异常均形成固定安全终态；回放不重复外部调用。

## 自动化验证

- 最终核心复审新增 6 个参数化 case：生产修改前 Tool retry 为 `3 failed, 1 passed`，Chat NEEDS_INPUT 为 `1 failed, 1 passed`；生产修改后合计 `6 passed in 3.91s`。
- 最终核心三文件聚焦套件（M8 Tool retry、M8 message idempotency、Result commit）：`69 passed in 33.69s`。
- 第二轮新增/改写的 13 个回归用例在生产修改前为 `13 failed in 8.38s`；失败分别对应 Chat finalize、初始 ToolRun、Runtime success/failure、initial/retry Result、initial Explanation prepare/finalize、retry Explanation finalize 以及 selected BOTH_NULL 两条路径。生产修改后同组为 `13 passed in 9.11s`。
- 第二轮直接受影响的 M8 message/tool/explanation、Explanation outcomes、Message workflow、Result 与 Explanation unit 聚焦套件：`90 passed in 49.13s`。
- Backend 完整套件：`771 passed in 101.92s`，0 failed、0 skipped。
- 并发消息重复压力：10 轮，每轮 `2 passed`；总计 20 次 pytest case execution。
- 完整 Tool TASK_CREATE 20 路并发专项连续 3 轮：每轮 `1 passed`。
- Mock Runtime：`11 passed in 1.13s`。
- Backend 环境 `pip check`：`No broken requirements found.`。
- `python -m compileall -q backend/src mock-runtime/src`：exit 0。
- 当前本地 PostgreSQL：唯一 head/current 为 `0008_idempotency_record (head)`；`alembic check` 为 `No new upgrade operations detected.`。
- nullable expiry 迁移往返：`upgrade head → downgrade 0007_tool_result_explanation → upgrade head` 成功；自动化也覆盖 `expires_at=NULL` 的 0008 downgrade-only round trip。
- `check-scope.ps1 -Milestone M8`：`SCOPE_OK M8`。
- `check-sem-integrity.ps1`：`SEM_INTEGRITY_OK`，57 files，`total_size_bytes=2043071133`，fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- `git diff --check`：通过。

## 真实 HTTP / PostgreSQL / MinIO / Mock Runtime 人工验收

使用随机一次性 PostgreSQL 数据库、真实已配置 MinIO bucket、仓库 Mock Runtime loopback HTTP 服务、Backend TestClient 公共 HTTP 路由和 Mock Explanation Adapter。

| 场景 | HTTP / 状态 | 幂等与引用 | Adapter 调用变化 | 数据库事实 |
|---|---|---|---|---|
| A NEW_TASK replay | 200 → 200，Task SUCCEEDED | 同 Task/Result，replay=true | Runtime +0，Storage put +0 | 1 Task/Run/Asset/Result/Explanation/record |
| B 同 key 异内容 | 409 `IDEMPOTENCY_CONFLICT` | 原资源不变 | 全部 +0 | Task/Message/ToolRun 等计数不变 |
| C Supplement replay | 200 → 200，Task NEEDS_INPUT | replay=true | replay 时 Provider +0 | 首次只增 1 User Message、1 Assistant Message、1 Revision、1 record；replay +0 |
| D Tool retry | 200，Task SUCCEEDED | attempt 1 → 2，新 seed、新 Run/Asset/Result；selected references 指向新结果；旧 Result GET 200 | Runtime HTTP +1，Storage put +1 | 两个 attempt 与两个不同 seed 均保留 |
| E Tool retry replay | 200 | 同 tool_run_id，replay=true | Runtime +0，Storage put +0 | 所有行数不变 |
| F Explanation retry | 200，Task SUCCEEDED | attempt 1 FAILED → attempt 2 SUCCEEDED；新 LLMCall/Explanation、同 Result，replay=false | Explanation +1，Runtime +0，Storage +0 | 该 Result 保留 2 个 Explanation attempt |
| G Explanation conflict | 409 `IDEMPOTENCY_CONFLICT` | 相同 key 改 reason | 全部 +0 | 所有行数不变 |
| H 所有权隔离 | GET Task/Result 与两个 retry 均 404 | 统一 `RESOURCE_NOT_FOUND` | Runtime/Storage/Explanation 全部 +0 | 所有行数不变 |

本次验收总计 Chat Provider 5 次、Mock Runtime HTTP execute 3 次、MinIO put 4 次、Explanation Provider 5 次。结束后删除本轮 4 个 MinIO 对象、丢弃一次性数据库并停止 Runtime；未删除命名 volume，未使用 `docker compose down -v`。

## M8 实际修改文件

### 生产代码

- `backend/src/materialsagent/api/dependencies.py`
- `backend/src/materialsagent/api/routes/conversations.py`
- `backend/src/materialsagent/api/routes/tasks.py`
- `backend/src/materialsagent/api/routes/tool_results.py`
- `backend/src/materialsagent/application/chat_orchestration.py`
- `backend/src/materialsagent/application/errors.py`
- `backend/src/materialsagent/application/explanation_service.py`
- `backend/src/materialsagent/application/idempotency.py`
- `backend/src/materialsagent/application/messages.py`
- `backend/src/materialsagent/application/result_service.py`
- `backend/src/materialsagent/application/retries.py`
- `backend/src/materialsagent/application/tool_execution.py`
- `backend/src/materialsagent/application/tool_workflow.py`
- `backend/src/materialsagent/domain/models/idempotency_record.py`
- `backend/src/materialsagent/domain/ports/unit_of_work.py`
- `backend/src/materialsagent/infrastructure/db/actor.py`
- `backend/src/materialsagent/infrastructure/db/conversation_task.py`
- `backend/src/materialsagent/infrastructure/db/idempotency_record.py`
- `backend/src/materialsagent/infrastructure/db/tool_result.py`
- `backend/src/materialsagent/infrastructure/db/unit_of_work.py`
- `backend/src/materialsagent/main.py`

### Migration

- `backend/alembic/env.py`
- `backend/alembic/versions/0008_create_idempotency_record.py`

### 测试

- `backend/tests/api/test_assets.py`
- `backend/tests/api/test_conversations.py`
- `backend/tests/api/test_explanation_outcomes.py`
- `backend/tests/api/test_m8_explanation_retry.py`
- `backend/tests/api/test_m8_message_idempotency.py`
- `backend/tests/api/test_m8_tool_retry.py`
- `backend/tests/api/test_message_orchestration.py`
- `backend/tests/api/test_tasks.py`
- `backend/tests/api/test_tools.py`
- `backend/tests/integration/db/test_idempotency_persistence.py`
- `backend/tests/integration/db/test_migrations.py`
- `backend/tests/integration/db/test_result_commit.py`
- `backend/tests/unit/test_explanation_service.py`
- `backend/tests/unit/test_idempotency.py`

### 脚本与动态状态

- `scripts/dev/check-scope.ps1`
- `docs/progress/phase-1-current-status.md`

以上 39 个路径全部属于 M8 allowlist；暂存区为空。五份设计基线、`0001`–`0007`、Mock Runtime 和 `SEM/` 均未修改。

## 已知风险

- PostgreSQL 与 MinIO 之间仍不是分布式事务；M8 沿用 M6 已验收的 PENDING、HEAD 验证、补偿删除和 ORPHANED 恢复边界，没有扩大该风险。
- IdempotencyRecord 目前不设置自动过期或清理策略；阶段 1A 本地 MVP 会持续保存这些审计/回放锚点。引入清理必须作为后续明确设计，而不是在 M8 内隐式删除。
- 回放返回“当前稳定业务投影”而不是逐字节重放历史 HTTP body，因此 task/explanation 的后续合法状态变化会反映在同 key 回放中；资源绑定 ID 保持不变。

## 下一步

仅等待项目负责人代码审查。未经明确批准，不暂存、不提交、不 push、不 amend，也不开始 M9。

```text
M8_FINAL_CORE_REVISION: COMPLETE
M8_PROJECT_OWNER_REVIEW: AWAITING
Commit: NO
Push: NO
Amend: NO
M9: NOT STARTED
```
