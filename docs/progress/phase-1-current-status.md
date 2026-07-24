# 阶段 1 当前执行状态

> 本文件只保存当前动态状态、最近工作单元证据和下一步。产品与架构语义仍以五份已确认设计基线和阶段 1 实施计划为准。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M10：最小 Vue 3 + Vite 前端 |
| 当前工作单元 | M10-A：前端基础与可靠数据层 |
| 状态 | `M10_A_PROJECT_OWNER_ACCEPTED_COMMIT_AUTHORIZED` |
| 上一已验收工作单元 | M10-A：前端基础与可靠数据层 |
| Pre-M8 stop-loss commit | `891714dd58cf069073a7d4037be43ef66304d0ac` |
| M8 | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M8 acceptance commit | `18005944982ca5191412e06154effc67465ca3a7` |
| M9 | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M9 acceptance commit | `d7dee06c1f1294b010f64f5c532f5cb276ca53fa` |
| M10-A acceptance commit | `PENDING` |
| 实际 branch / HEAD | `main` / `d7dee06c1f1294b010f64f5c532f5cb276ca53fa` |
| HEAD parent / subject | `18005944982ca5191412e06154effc67465ca3a7` / `feat: add stable conversation timeline` |
| 暂存区 | M10-A 精确 28 个批准路径 |
| 当前工作区 | M10-A 已获项目负责人验收并授权 commit；批准内容已精确暂存，commit 尚未创建 |
| 已确认设计基线 | 五份均未修改 |
| 历史 migration | `0001`–`0008` 均未修改；当前唯一 head/current 为 `0009_timeline_query_indexes` |
| `SEM/` | 未修改、未加载或运行真实模型；`SEM_INTEGRITY_OK` |
| Mock Runtime | 实现和协议未修改 |
| Git 外部动作 | Commit 已授权但尚未创建；未 push、未 amend、未 rebase、未 reset、未 stash |
| M10-A | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M10-B | `NOT STARTED` |
| M11 | `NOT STARTED` |
| 是否处于项目负责人暂停点 | 是；只允许后续创建已授权的 M10-A 本地验收 commit，不得 push、amend 或进入 M10-B |
| 更新时间 | `2026-07-24` |

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

## M9 已验收执行记录

- M9-P 基线 gate：`main@18005944982ca5191412e06154effc67465ca3a7`，暂存区与工作区均为空，`git diff --check` 与 `git diff --cached --check` 通过。
- Docker：PostgreSQL 与 MinIO 均为 healthy；命名 volumes 为 `materialsagent_postgresql_data` 与 `materialsagent_minio_data`，未删除。
- M9-P 已把本次“同一对话连续完成 M9-P、M9-A、内部 gate、M9-B 与全量验证”的负责人例外、各段边界、验收命令和精确 allowlist 写入阶段 1 计划与 `check-scope.ps1`。
- M9-A 新增版本 1 的 HMAC-SHA256 不透明 cursor、可选 `SecretStr` 签名密钥、Timeline Query Port/SQLAlchemy Adapter、数据库 keyset 与 `limit + 1`、固定批量查询及 `REPEATABLE READ READ ONLY` 快照。
- cursor 使用规范 JSON、base64url 和常量时间签名比较，绑定 Conversation，并限制 token 最大 2048 个字符、解码后的 canonical JSON payload 最大 512 bytes；完整保留数据库 UTC 微秒精度。密钥缺失只使 Timeline 返回安全 503。
- Alembic：新增 `0009_timeline_query_indexes`，`down_revision=0008_idempotency_record`；唯一 head/current 为 `0009_timeline_query_indexes`，`alembic check` 无待生成操作，`0009 → 0008 → 0009` 往返成功。
- 索引为 `ix_message_conversation_created(conversation_id, created_at, message_id)` 与 `ix_task_conversation_created(conversation_id, created_at, task_id)`。
- M9-A 内部 gate 在开始路由前通过：基线、allowlist、cursor/config/sort/query、固定 9 条 SELECT、只读可重复读、migration 往返、scope 与 diff checks 均满足。
- M9-B 新增 `GET /api/v1/conversations/{conversation_id}/timeline`：顶层只有 `USER_MESSAGE`、`ASSISTANT_MESSAGE`、`TOOL_TASK`；Tool 初始用户消息折叠进卡片，不在顶层重复。
- 顶层顺序固定为 `(anchor_at, item_type_rank, item_id)`，rank 为 10/20/30；Tool anchor 取最早用户消息，缺失时安全回退 Task.created_at。Timeline Tool 卡片只返回 `attempt_count`、`selected_tool_run`、`has_history`，并投影 selected Result/Assets/Explanation 和最多 50 条最近输入；完整 ToolRun 历史由 Task GET 返回。
- Task GET 通过短 `REPEATABLE READ READ ONLY` 事务的一致快照投影稳定 anchor、输入、全部 ToolRun、selected Result/Assets/Explanation、needs-input 与安全失败；Timeline 与 Task GET 都执行 Actor 所有权和 selected 来源一致性校验，不调用 Chat、Runtime、MinIO 或 Explanation Provider。
- 数据库 adapter 对 1 个和 20 个 Tool Task 均固定执行 9 条 SELECT，没有 N+1；并发插入测试证明一次响应保持单个可重复读快照，下次请求才看到新事实。
- 五份确认设计基线、`SEM/`、Mock Runtime 均未修改；未加载或运行真实模型。

## M9 第一轮统一代码审查修订证据

- Task GET 已从普通 READ COMMITTED UoW 多次读取改为专用 `TaskDetailQuerySnapshot`：一次 Service 调用只进入一次 query port，并在单个短 `REPEATABLE READ READ ONLY` 事务中读取完整 Task detail。
- Task、Messages、Revisions、ToolRuns、selected ToolResult、ResultAssetLink JOIN Asset、Explanation JOIN LLMCall 使用固定 7 条业务 SELECT 批量读取；1 attempt / 1 Asset / 1 Explanation 与 20 attempts / 8 Assets / 20 Explanations 均为 7 条业务 SELECT、10 条总语句，不随资源数量增长。
- Tool retry 并发测试在第一批查询建立快照后，由另一连接提交新 ToolRun、Result、Asset 和 selected references：当前响应完整保持旧链，下一次查询才完整看到新链和两次 ToolRun 历史。
- Explanation retry 并发测试在第一批查询建立快照后，由另一连接提交新 LLMCall、Explanation 和 Task 聚合状态：当前响应保持旧 Task/Explanation，下一次查询才同时看到新 Task 状态和成功 Explanation。
- 查询 Session 使用上下文管理器显式关闭；transaction 在成功、未找到和异常路径均回滚结束，connection 在 `finally` 中关闭。Session close 回归测试覆盖成功、未找到和异常三条路径。
- Task GET 公共字段、完整 ToolRun 历史、selected Result/Asset/Explanation、来源损坏安全 500、其他 Actor/缺失资源统一 404、数据库 unavailable 安全 503 均保持；缺少 Timeline signing key 时 Task GET 200、Timeline 503。
- 第一轮审查结论：Original Critical 0；Original Important 1 FIXED；Original Important 2 FIXED；Session close Minor FIXED；Route coupling Minor DEFERRED。

## M9 自动化验证

- Task query 单元：`6 passed in 0.20s`。
- Task detail/Timeline DB：`8 passed in 3.20s`。
- Task API：`7 passed in 3.22s`。
- M9 全部聚焦：`118 passed in 16.85s`。
- 全部 contract：`87 passed in 1.49s`。
- Backend 全量：`848 passed in 125.93s`，0 failed、0 skipped。
- Mock Runtime：`11 passed in 1.13s`。
- Backend `pip check`：`No broken requirements found.`；`compileall`：exit 0。
- Alembic：唯一 head/current 为 `0009_timeline_query_indexes (head)`；check clean；`0009 → 0008 → 0009` 成功且最终回到 0009。
- `check-scope.ps1 -Milestone M9`：`SCOPE_OK M9`。
- `check-sem-integrity.ps1`：`SEM_INTEGRITY_OK`，57 files，`total_size_bytes=2043071133`，fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。

## 已知风险

- Timeline cursor 签名密钥目前由本地环境配置提供；缺失时该单一路由按设计返回 503。部署或换机时必须用至少 32 UTF-8 bytes、无首尾空白和控制字符的 Secret 配置，不能提交真实值。
- Timeline 是跨多表的稳定读取投影，但并不冻结两次 HTTP 请求之间的业务状态；单次请求由只读可重复读快照保证一致，后续请求可合法看到 retry 或 Explanation 新事实。
- PostgreSQL 与 MinIO 之间既有非分布式事务边界仍然存在；M9 只读取已经持久化且通过来源一致性校验的 AVAILABLE Asset，没有扩大或掩盖该风险。

## M10-A 当前执行记录

- 已按实时 Git 修正 M9 为 `COMPLETE / PROJECT_OWNER_ACCEPTED`，验收提交为 `d7dee06c1f1294b010f64f5c532f5cb276ca53fa`。
- M10-A 开始基线为 `main@d7dee06c1f1294b010f64f5c532f5cb276ca53fa`；开始时工作区与暂存区为空。
- 已在阶段 1 计划中确认 M10-A/M10-B 拆分，并在 `check-scope.ps1` 中加入仅覆盖本工作单元的 M10 精确 allowlist；管理文件内部 gate 为 `SCOPE_OK M10`、`SEM_INTEGRITY_OK`、两个 diff check 均通过。
- Node/npm 为 `v24.14.0` / `11.9.0`。直接依赖全部精确锁定：Vue `3.5.40`、Vite `8.1.5`、`@vitejs/plugin-vue` `6.0.8`、TypeScript `6.0.3`、`vue-tsc` `3.3.8`、Vitest `4.1.10`、Vue Test Utils `2.4.11`、jsdom `29.1.1`、`@types/node` `26.1.1`。
- `npm --prefix frontend ci` 成功，安装 166 packages，audit 为 0 vulnerabilities；存在 dev-only 传递依赖弃用提示：`@vue/test-utils@2.4.11 → js-beautify@1.15.4 → glob@10.5.0`。
- TDD 红测分别确认 API Client、幂等状态机、轮询协调器和应用级 composable 模块尚不存在；实现后聚焦结果依次为 `24 passed`、`21 passed`、`14 passed`、`30 passed`。最终 `npm --prefix frontend run test -- --run` 为 4 files、`89 passed`、0 failed。
- `npm --prefix frontend run typecheck` 为 0 errors；`npm --prefix frontend run build` 使用 Vite `8.1.5` 成功构建 17 modules，生成的 `dist` 仅为被忽略的本地产物。
- 实现了原生 fetch Client、公共响应/错误类型、安全错误投影、写请求 Idempotency-Key、单个 sessionStorage 待定写操作、UNCERTAIN 恢复与同 key/body 手动重试、Timeline `limit=50` 全分页原序原子替换、AbortController + generation 陈旧响应保护、递归 timeout 单轮询协调器和应用级 `useMaterialsAgent`。
- 最小浏览器验收已通过：Vite 在 `127.0.0.1:4173` 启动，页面显示“材料智能体”“M10-A 前端基础已就绪”“前端数据层已加载”，API base 为 `/api/v1`，mutation 状态为 `IDLE`，控制台 0 warning/error；未触发真实 API 写请求，验收后已停止 dev server。未对 Backend proxy 进行可选的在线只读验证。
- 最终 `check-scope.ps1 -Milestone M10` 为 `SCOPE_OK M10`；SEM 为 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；`git diff --check` 与 `git diff --cached --check` 均为 exit 0。
- production source 与 `frontend/dist` 对 `object_key`、bucket/MinIO、Runtime token、Timeline signing key、PostgreSQL URL、weight path、用户绝对路径和 `SEM/` 的有界扫描均无命中；无 Backend、五份设计基线、SEM 或 M10-B component diff。
- 修改文件仅为三个管理文件与 `frontend/` M10-A allowlist 文件：前端环境/包与 TypeScript/Vite 配置、最小 App/CSS、API types/errors/client、三个 composable、测试 setup 和四个测试文件。`node_modules`、`dist`、coverage、其他 lockfile 均未进入 Git 状态。
- 已知风险：M10-A 仍是最小占位页而非完整产品 UI；手写 TypeScript 类型需要在 Backend 公共契约变化时同步；sessionStorage 的待定恢复只覆盖同一标签页；上述 dev-only `glob@10.5.0` 弃用提示等待上游依赖链更新。
- 当前只执行 M10-A：前端基础与可靠数据层；M10-B 和 M11 均未开始。
- 未执行 `git add`、commit、push、amend、rebase、reset、stash、分支或 worktree 操作。

## M10-A 首轮代码审查修订证据

- 首轮结论为 `M10_A_CODE_REVIEW: CHANGES_REQUESTED`；本轮仅修改审查允许的六个生产文件、四个测试文件和本动态进度文件，没有创建新生产文件、修改依赖或进入 M10-B。
- Critical 1 根因是 POST 与写后 GET reconciliation 位于同一 `try/catch`，导致已确认写入被后续读取失败重新报告为写失败。现已把服务器确认与 best-effort reconciliation 分成两个阶段：POST 成功立即固定 `SUCCEEDED`、清 pending、保存 POST request_id、执行 cache invalidation/补参目标清理；后续 GET 失败只显示固定安全刷新提示且不重新抛出。
- Critical 2 根因是切换 Conversation ID 时沿用旧 Timeline，目标 Conversation 首次 GET 失败后仍会展示旧 Conversation 数据。现已在真实 ID 切换时立即清空 Timeline；同一 Conversation 的普通 refresh 失败仍保留现有完整 Timeline。
- 红测：API Client 为 `6 failed, 29 passed`，失败覆盖 GET/Conversation 网络分类和 Asset URL 白名单；幂等状态机为 `7 failed, 21 passed`，覆盖四类 body 校验、255 字符边界和 Unicode control；轮询为 `1 failed, 14 passed`，实际错误为 3 次 poll 而预期 2 次；应用级 composable 为 `13 failed, 30 passed`，覆盖 POST/GET 分离、Conversation 混用、Task history 竞态、UNCERTAIN 保留和 scope dispose；`warnings: unknown[]` 类型红测产生 2 个 `TS2344`。
- Task history 现按 `task_id` 使用 AbortController + generation；retry invalidation 会 abort/失效旧 GET，较早并发请求不再提前清除较新请求的 loading 状态。
- API Client 现区分读取网络失败、四类幂等写结果不确定和 Conversation 创建结果不确定；Conversation 创建固定提示先刷新列表避免重复创建，不自动重试。
- sessionStorage descriptor 已改为 operation-specific 判别联合并按 operation 严格校验 body；损坏记录安全清除且不发送。Idempotency-Key 允许最大 255 字符，并拒绝 Unicode control character。
- Asset 公共 URL 只接受 `/api/v1/assets/{asset_id}/content` 及其 query；拒绝第三方绝对 URL、protocol-relative、javascript/data 和其他公共路径。绝对 API base 只提供解析 origin，attachment 保留既有 query。
- 有效 Vue scope dispose 会停止 polling、移除 visibility listener，并 abort Conversation、Timeline 和全部 Task history GET；generation 同时阻止不响应 abort 的迟到响应写回状态。
- hidden 时会清除 queued poll trigger，恢复 visible 后只执行一次立即 poll。成功后台 GET 不再清除仍需用户处理的 `UNCERTAIN` 提示。
- Vue 在 `package.json`、`package-lock.json` 和 `node_modules` 三处均为 `3.5.40`；直接依赖版本未修改。`npm ci` 成功，audit 为 0 vulnerabilities，保留既有 dev-only `glob@10.5.0` 上游弃用提示。
- 修复后聚焦结果为 API Client `36 passed`、幂等状态机 `28 passed`、轮询 `15 passed`、应用级 composable `43 passed`；全量为 4 files、`122 passed`、0 failed。
- 最终 `npm --prefix frontend run typecheck` 为 0 errors；Vite `8.1.5` build 成功、17 modules；`SCOPE_OK M10`；SEM 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；两个 diff check 均为 exit 0。
- 未发现 `as unknown as`、生产 `any` 或 skipped tests；暂存区为空，未执行 commit、push 或 amend。

## M10-A 最终代码复审修订证据

- 最终复审结论为 `M10_A_FINAL_CODE_REVIEW: CHANGES_REQUESTED`；本轮只处理最后两个 Important 和一个 Minor，没有修改依赖、lockfile、计划、scope、Backend、CORS、设计基线、`SEM/`、Mock Runtime 或 M10-B 文件。
- 开始前已删除仓库根目录未跟踪审查传输文件 `m10-a-code-review-revision.zip`；随后及最终检查的根目录 `*code-review*.zip` 数量均为 0，Git 状态也没有 zip、`node_modules`、`dist` 或 coverage。
- 定向红测为 2 files、`8 failed / 81 passed`：Conversation 创建非 JSON 的 HTTP 200/500/503 共 3 项；discard 后旧提示 1 项；Conversation 切换期间 dispose 后 polling 复活 1 项；普通幂等写、pending retry 和 Conversation 创建在 POST 晚于 dispose 成功后仍触发 GET 共 3 项。
- `loadConversations`、Timeline 两个读取入口、`selectConversation`、Task history 和写后 reconciliation 现有明确 disposed 生命周期门；`selectConversation` 只在 `restartPolling && !disposed` 时恢复 polling，公开 `startPolling` 也不会在已销毁 scope 上启动。切换 B 的迟到响应不写 Timeline，推进 fake timers 60 秒没有新增 Timeline GET，visibility listener 的 add/remove 数量配平。
- 已发送的写 POST 没有取消或自动重试。服务器明确成功后，幂等状态机仍进入 `SUCCEEDED` 并清除 pending；若 scope 已 dispose，普通写和 pending retry 都不执行 cache invalidation、页面状态写回或 Timeline/Conversation reconciliation。Conversation 创建晚到成功同样直接返回已创建对象且不启动 GET。
- API Client 在 Conversation 创建响应无法解析 JSON 时，无论 HTTP 200、500 或 503，均抛出固定安全的 `ConversationCreationUncertaintyError`，提示先刷新列表以避免重复创建；普通 GET 和幂等写的非 JSON 响应仍是 `ProtocolResponseError`，不暴露原始 HTML 或响应正文。
- `discardPendingMutation()` 只有实际丢弃 pending descriptor 时才清除旧 `globalError`；成功丢弃后状态为 `IDLE`、pending 为 `null`、globalError 为 `null`，没有 pending 时保留无关错误。
- 本轮新增 10 项测试；定向绿测为 2 files、`89 passed`，全量为 4 files、`132 passed`、0 failed。`npm ci` 安装 166 packages、audit 0 vulnerabilities；保留既有 dev-only `glob@10.5.0` 上游弃用提示。typecheck 为 0 errors，Vite `8.1.5` build 成功、17 modules。
- `SCOPE_OK M10`；`SEM_INTEGRITY_OK` 为 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；两个 diff check 均为 exit 0。Vue 在 `package.json`、lockfile 和实际安装三处均为 `3.5.40`。
- 当前仍为 `main@d7dee06c1f1294b010f64f5c532f5cb276ca53fa`，暂存区为空；未执行 `git add`、commit、push 或 amend，M10-B 和 M11 均未开始。

## M10-A 项目负责人验收

- 项目负责人已验收 M10-A：`COMPLETE / PROJECT_OWNER_ACCEPTED`。
- M9 保持 `COMPLETE / PROJECT_OWNER_ACCEPTED`，acceptance commit 为 `d7dee06c1f1294b010f64f5c532f5cb276ca53fa`。
- M10-A acceptance commit 当前为 `PENDING`；项目负责人已授权创建该本地 commit，但本轮尚未创建。
- 暂存区精确包含 M10-A 的 28 个批准路径；没有 scope 外暂存路径。
- M10-B 与 M11 均为 `NOT STARTED`；未执行 push 或 amend。

## 下一步

下一步只允许创建已获授权的 M10-A 本地验收 commit；本轮尚不创建 commit，不 push、不 amend，也不进入 M10-B：

```text
M10_A_PROJECT_OWNER_ACCEPTED_COMMIT_AUTHORIZED

M9:
COMPLETE / PROJECT_OWNER_ACCEPTED
Acceptance commit:
d7dee06c1f1294b010f64f5c532f5cb276ca53fa

M10-A:
COMPLETE / PROJECT_OWNER_ACCEPTED
Acceptance commit:
PENDING

M10-B: NOT STARTED
Staged paths: 28
Commit: AUTHORIZED / NOT YET CREATED
Push: NO
Amend: NO
M11: NOT STARTED
```
