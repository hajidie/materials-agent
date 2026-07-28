# 阶段 1A 正式验收报告

状态：`PHASE_1A_PROJECT_OWNER_ACCEPTED_STAGING_AUTHORIZED`

报告日期：2026-07-28

项目负责人人工验收日期：2026-07-28

本报告是静态、可审计的 M0–M11 验收记录。动态命令日志、JUnit 和
`summary.json` 由统一入口写入被 Git 忽略的
`tmp/phase-1a-acceptance/<run-id>/`；本文件不由验收脚本自动改写。

## 1. 验收范围

- 范围：阶段 1A，M0–M11。
- 运行形态：完全 Mock；PostgreSQL、MinIO、Mock Runtime、Backend 与
  Frontend 组成同一套本地验收栈。
- 排除：真实材料模型、真实 LLM Provider、真实 LangChain 网络调用、GPU
  推理、生产部署和 M12。
- 验收状态：已确认的 MinIO Asset 生命周期生产缺陷和默认 Mock 补参缺陷均已
  完成最小修复；统一 Runner 权威验收与项目负责人 11 项浏览器清单均已通过。
  当前只授权精确暂存 M11-B 变更，尚未授权 commit。

## 2. Git 基线

- M11-B start baseline：
  `main@4ed740222238433541fb31c993dd75610634d157`。
- 基线主题：`feat: add reliable mock stack acceptance`。
- 基线父提交：`f426e6f23703002a648991e5bb436929df19e8e2`。
- 开始前工作区和暂存区均为空。
- 当前尚无 M11-B commit；本报告不声明或编造 M11-B commit hash。
- Runner 在任何 Mock Stack 启动前要求 branch 为 `main`、暂存区为空，并用
  `git merge-base --is-ancestor` 验证上述 M11-A commit 是当前 HEAD 的祖先；
  因此未来 M11-B commit 存在后仍可重跑。
- Runner 将实际 branch、HEAD、subject、parent 和祖先校验结果写入
  `summary.json`；错误分支、detached HEAD 或缺少 M11-A 基线均非零退出、
  仍写 summary，且不调用 start。

## 3. Mock Stack

验收栈包含：

- PostgreSQL：保存结构化事实；E2E 每个 session 使用本轮独有的临时数据库。
- MinIO：保存 PNG；E2E 每个 session 使用本轮独有的临时 bucket。
- Mock Runtime：仅提供 `zta35g_sem_virtual_lab` 的固定 Mock bundle。
- Backend：使用现有公共 API 和进程内 Mock Chat/Explanation adapter。
- Frontend：Vite 本地服务，通过既有代理访问 Backend。

统一入口只调用 `start-mock-stack.ps1` 和 `stop-mock-stack.ps1`。若发现经
M11-A 校验的既有栈则复用且不停止；若由 Runner 启动，则只在 finally
调用 stop。Runner 不复制 PID、CIM 或 Docker ownership 逻辑，不执行
`down -v`，不删除 volume。

## 4. 环境和版本

| 项目 | 验收版本 |
|---|---|
| Python | 3.11.15 |
| Node | 24.14.0 |
| npm | 11.9.0 |
| Docker Compose | 5.3.0 |
| PostgreSQL image | `postgres:17.10-bookworm`，Compose 固定 digest |
| MinIO image | `minio/minio:RELEASE.2025-09-07T16-13-09Z`，Compose 固定 digest |

本表全部证据来自统一 Runner 的独立 command record 与 `summary.json`，
不是额外人工版本命令。Runner 同时记录 `docker compose config --services`
的精确集合 `minio, postgresql`，以及与当前 Compose 固定配置完全一致的两条
带 digest image。报告和动态证据均不记录 Python、Node、Docker 或仓库的
本机绝对安装路径。

## 5. Alembic

- `alembic heads`：唯一 head 为
  `0009_timeline_query_indexes (head)`。
- `alembic current`：当前数据库为
  `0009_timeline_query_indexes (head)`。
- `alembic check`：`No new upgrade operations detected.`。
- M11-B 没有增加或修改 migration。

## 6. 自动化命令

稳定入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/acceptance/run-phase-1a.ps1
```

入口依次执行 Git baseline/status、权威环境版本与 Compose 配置、Scope M11、
SEM pre-check、Mock Stack start/reuse、四个 HTTP 健康面、Alembic、带 JUnit
的 M11 E2E、Backend
全量、Mock Runtime 全量、`pip check`、`compileall`、Frontend Vitest、
typecheck、build、Scope M11/M11B、SEM post-check、Git diff/index 和安全
扫描。阶段 1A 最新权威验收（run id
`20260728T075704Z-c0e323e1c2bf`）结果为：

- M11 E2E：24 passed（M11-A 4 + M11-B 20）。
- Backend 全量：882 passed。
- Mock Runtime：11 passed。
- Frontend：9 files、204 passed；typecheck 和 build exit 0。
- `pip check`：无破损依赖；`compileall` exit 0。
- 场景统计为 success 1、input error 5、dependency failure 7、
  idempotency 5、retry 2、asset/security 2。
- `PHASE_1A_ACCEPTANCE_PASSED`、`failed=0`、
  `browser_manual_scenarios=11`。

权威验收摘要为：

```text
PHASE_1A_ACCEPTANCE_PASSED
run_id=20260728T075704Z-c0e323e1c2bf
success_scenarios=1
input_error_scenarios=5
dependency_failure_scenarios=7
idempotency_scenarios=5
retry_scenarios=2
asset_security_scenarios=2
browser_manual_scenarios=11
failed=0
```

TDD 修复前，正确 MinIO 聚焦用例曾得到以下有效 RED：

```text
actual Asset status = PENDING
expected Asset status = FAILED
```

该历史 RED 发生在状态断言；在此之前 Runtime `execution_count=1`、Task/ToolRun
`FAILED`、无 ToolResult、无 ResultAssetLink、无 MinIO object 和公共响应无
`object_key` 泄漏均已通过。修复后同一用例、聚焦 Storage/Asset 集合、
完整 E2E 及统一 Runner 均已通过；上述 RED 只保留为 TDD 证据，不是当前状态。

## 7. 场景矩阵

| 场景 | 预期 | 实际 | 证据来源 | 状态 |
|---|---|---|---|---|
| 部分成功 | SEM image 可用、性能失败、说明不虚构 MPa | Task/Result 为 `PARTIALLY_SUCCEEDED`，一次 Runtime 调用 | M11-B E2E + JUnit | PASS |
| 空 content | 422，零资源与外调 | 所有计数不变，Runtime=0 | M11-B E2E | PASS |
| 非法单位 | 422，不进 Runtime/MinIO | Run/Result/Asset/Object 均不增 | M11-B E2E | PASS |
| 参数越界 | 422，不进 Runtime/MinIO | Run/Result/Asset/Object 均不增 | M11-B E2E | PASS |
| 不支持材料 | 422，不进 Runtime/MinIO | Run/Result/Asset/Object 均不增 | M11-B E2E | PASS |
| 非法 requested outputs | 422，不进 Runtime/MinIO | Run/Result/Asset/Object 均不增 | M11-B E2E | PASS |
| PostgreSQL unavailable | 安全 503、无副作用 | `DEPENDENCY_UNAVAILABLE`，全计数不变 | M11-B E2E | PASS |
| MinIO pre-write unavailable | Task/ToolRun 安全失败；无 Result/link/object；Asset 为 `FAILED` | Asset `FAILED` 且安全错误事实非空；Task/ToolRun `FAILED`；无 Result/link/object | 最新 M11-B E2E + Runner | PASS |
| MinIO write outcome unknown | `put_object` 已开始后保持可恢复事实 | Asset 保持 `PENDING`，不伪造 AVAILABLE/FAILED，等待 recovery | Storage/Asset 回归测试 | PASS |
| Runtime unavailable | 一个失败 Run，无 Result/Asset | 503 `RUNTIME_UNAVAILABLE`，无 Runtime execute | M11-B E2E | PASS |
| Runtime timeout | 拒收迟到结果 | 504；一次 execute，无 Result/Asset/object | M11-B E2E | PASS |
| Runtime busy | 有界失败且不自动重试 | 503 `RUNTIME_BUSY`，execute count 保持 1 | M11-B E2E | PASS |
| Explanation failure | 已提交 Result/Asset 保留 | Task 部分成功，Explanation 失败 | M11-B E2E | PASS |
| Explanation timeout | 已提交 Result/Asset 保留 | Task 部分成功，Explanation 失败 | M11-B E2E | PASS |
| TASK_CREATE replay | 返回同一事实，零增量 | replay flag=true，资源/外调不增 | M11-B E2E | PASS |
| 补参 replay | 只产生一组补参事实 | Message/Revision/Run/Result 各只增一次 | M11-B E2E | PASS |
| 同 key 不同 digest | 409、零副作用 | `IDEMPOTENCY_CONFLICT`，资源/外调不增 | M11-B E2E | PASS |
| Tool retry/replay | 新 Run 一次；重放零增量 | attempt 1/2 保留，第二次选中，重放不再调用 | M11-B E2E | PASS |
| Explanation retry/replay | 同 Result，新说明；重放零增量 | Explanation/LLMCall 各新增一次，Runtime 不变 | M11-B E2E | PASS |
| Asset inline/attachment | PNG 相同且安全 header | 两模式均 200、PNG magic 一致 | M11-B E2E | PASS |
| 跨组件泄漏 | 公共 JSON/header 无内部值 | Secret、object key、内部 URL/路径均缺席 | M11-B E2E | PASS |

最新权威 JUnit 名称前缀统计为：

```text
success_scenarios=1
input_error_scenarios=5
dependency_failure_scenarios=7
idempotency_scenarios=5
retry_scenarios=2
asset_security_scenarios=2
browser_manual_scenarios=11
```

`idempotency_scenarios=5` 包含四类 operation 的 replay，以及一个同
key/different digest 冲突场景；Tool/Explanation replay 同时计入对应 retry
类别。上述统计与最新 Runner 的 `failed=0` 共同构成当前自动化证据。

## 8. 幂等资源计数

| Operation | 首次操作的事实 | 同 key/body replay 资源变化 | Runtime 变化 | MinIO 对象变化 |
|---|---|---|---|---|
| TASK_CREATE | 创建一套 Task/Run/Result/Asset 事实 | 所有表计数 `+0` | `+0` | `+0` |
| TASK_INPUT_SUPPLEMENT | Message/Revision/Run/Result 各 `+1` | 所有表计数 `+0` | `+0` | `+0` |
| TOOL_RETRY | 新 ToolRun/Result/Asset 各 `+1` | 所有表计数 `+0` | `+0` | `+0` |
| EXPLANATION_RETRY | Explanation/LLMCall 各 `+1` | 所有表计数 `+0` | `+0` | `+0` |

四类 operation 都比较了 Actor/Conversation/Message/Task/Revision/ToolRun/
Result/Asset/Link/Explanation/LLMCall/IdempotencyRecord 行数、Runtime
`execution_count` 和 MinIO object count；不是只比较 HTTP body。

## 9. Tool retry

- Task：失败与成功尝试属于同一 `task_id`。
- ToolRuns：保留 attempt 1 `FAILED` 及其
  `INTERNAL_RUNTIME_ERROR`；新增 attempt 2 `SUCCEEDED`，seed 不同。
- 旧结果：首次 Runtime 失败路径没有伪造 Result；旧失败 Run 事实完整保留。
  唯一成功 Result/Asset 只归属于第二个 Run。
- selected 引用：Task 的 selected ToolRun/Result 均指向第二次成功事实。
- Timeline：同一 Task 卡片 `item_id` 与 `anchor_at` 在重试前后不变；
  attempt count 从 1 变 2。
- Runtime：初始失败一次、显式重试一次，共 2 次；同 key replay 不产生第
  3 次调用。
- Replay：重试后的全部数据库计数和 MinIO object count 不变。

## 10. Explanation retry

- Result：重试前后使用同一成功 `result_id`。
- Explanations：attempt 1 `FAILED` 与 attempt 2 `SUCCEEDED` 均保留。
- LLMCalls：每次 Explanation 尝试各一条，共 2 条。
- Runtime、ToolRun、Result、Asset、ResultAssetLink 和 MinIO object count：
  Explanation retry 前后完全不变；Runtime `execution_count` 始终为 1。
- Timeline：Task 卡片 `anchor_at` 不变。
- Replay：同 key replay 不再增加 Explanation/LLMCall，不重复外调。

## 11. 故障矩阵

- PostgreSQL：使用未监听的本地端口和 1 秒 test-only connect timeout；
  返回安全 503，未写入任何资源。
- MinIO pre-write unavailable：使用未监听的 loopback endpoint；Runtime
  执行一次，Asset 以非空安全错误事实终结为 `FAILED`，Task/ToolRun 安全
  失败，Result、Asset link 与 object 均未发布。
- MinIO write outcome unknown：`put_object()` 已开始后遇到无法确认写入结果
  的异常，Asset 保持 `PENDING`，不伪造 `FAILED`，等待既有 recovery/HEAD。
- Runtime unavailable：真实 loopback HTTP 目标不可达；只保留一个失败
  ToolRun。
- Runtime timeout：测试 HTTP server 等待受控 release；Backend 超时后迟到
  结果不落库。
- Runtime busy：一个请求占用单并发 Runtime，第二个请求得到
  `RUNTIME_BUSY`，Backend 不自动重试。
- Explanation failure/timeout：两个模式各一例；已提交的 Result/Asset 保留，
  Explanation 安全失败。

所有 Runtime 行为都经动态 `127.0.0.1` HTTP 端口进入现有 HTTP client
边界；没有用进程内 fake 替代 Runtime HTTP 协议。

### MinIO Asset 生命周期生产调用链

1. `POST /api/v1/conversations/{id}/messages` 完成消息与编排后调用
   `ToolWorkflowService.execute()`。
2. `ToolExecutionService.execute_revision_with_output()` 在 Runtime 成功后，
   以独立短事务把 Runtime safe output 记录到仍为 `RUNNING` 的 ToolRun。
3. `AssetService.create_from_output()` 调用 `_create_pending()`；该函数在一个
   独立 UoW 中构造 `Asset.pending()`，经 `AssetRepository.add()` 提交
   `PENDING`。
4. `_finalize()` 在 UoW 外 decode/encode，并调用
   `MinioStorageService.put()`。本轮不可达端点在 `put()` 的前置
   `head()` 连接阶段抛出 `StorageUnavailableError`，当前调用没有执行
   `put_object()`，对象不可能由本次调用上传。
5. `_finalize()` 先捕获更具体的 `StorageWriteOutcomeUnknownError`：保持
   Asset `PENDING` 并返回既有安全 503；普通 `StorageUnavailableError`
   则调用 `_persist_failed()`，只有失败事实事务提交成功后才返回既有安全
   `DependencyUnavailableError`。
6. `ToolWorkflowService.execute()` 捕获该 `ApplicationError` 后调用
   `_terminalize_asset_failure()`，在另一短事务把 Task 和 ToolRun 终结为
   `FAILED`，不创建 Result/link。

现有 Domain 已有 `Asset.mark_failed()`；`SQLAlchemyAssetRepository.update()`
也已支持带 `expected_status` 的 `PENDING/ORPHANED -> FAILED` 条件更新，
`AssetService._persist_failed()` 已封装安全失败事实提交。本轮新增内部
`StorageWriteOutcomeUnknownError` 表达 write certainty，并让明确的 pre-write
连接失败进入已有失败事实路径；schema、公共 API 与 ToolWorkflow 均未改变。

实际采用的最小生产 allowlist：

- `backend/src/materialsagent/domain/ports/storage.py`
- `backend/src/materialsagent/infrastructure/storage/minio.py`
- `backend/src/materialsagent/application/asset_service.py`

实际回归测试路径：

- `backend/tests/integration/storage/test_asset_lifecycle.py`
- `backend/tests/unit/test_storage_contract.py`
- `backend/tests/integration/storage/test_minio_storage.py`
- `backend/tests/e2e/test_mock_acceptance_matrix.py`

处理必须精确区分三类结果：

- 明确连接失败、对象不可能已上传：例如本轮前置 HEAD 连接失败，现以已有
  条件更新提交 Asset `FAILED`，并继续安全终结 Task/ToolRun。
- 上传结果不确定、需要 HEAD/recovery：`put_object()` 已开始后遇到超时或
  连接中断，不直接标记 FAILED；保留可恢复状态，通过 HEAD、ownership
  与 metadata 核验决定 AVAILABLE、FAILED 或 ORPHANED。
- 数据库状态终结也失败、只能留下 stale PENDING：失败事实的条件更新或
  commit 本身失败时不伪造 FAILED；传播安全持久化错误并保留恢复入口。

`migration required: NO`。现有表约束、字段、domain transition 和 repository
条件更新已经支持该终态。`public API change required: NO`；成功和安全错误
公共契约无需改变，只修正内部持久化事实及 storage 异常分类。

## 12. Asset 和安全

- inline 与 attachment 都返回精确 `image/png`、受控 filename 和相同 PNG
  bytes；attachment 仅改变 Content-Disposition。
- 公共提交响应、Task、Result、Timeline 和 Asset header 联合检查实际
  PostgreSQL/MinIO/Runtime/Timeline Secret 值、实际 object key、bucket、
  endpoint 和仓库路径均不出现。
- `data_base64`、完整 Prompt、traceback、SQL、模型路径和 bundle 内部身份
  不进入公共响应。
- Runner 日志先按实际 Secret 值、Windows 绝对路径和 PID 清洗，再扫描精确
  17 个 M11-B allowlist 路径与动态日志；范围包括原 15 路径，以及默认
  Mock responder 生产文件和 Chat orchestration contract test。原范围中的
  3 个 Storage 生产修复文件、3 个 Storage/Asset 回归测试文件和其余
  M11-B 脚本、E2E、文档继续保留。
  动态扫描同时覆盖 logs、`commands.json`、`summary.json` 和 JUnit XML；
  `commands.json` 只含命令名、时间、退出码和相对日志路径。
- 扫描覆盖探针结果为 `ACCEPTANCE_SCAN_COVERAGE_OK count=17`；Runner 在
  Mock Stack 启动和高成本测试前验证路径数、重复项、文件存在性及与 M11-B
  预期集合的一致性。`summary.json` 只记录安全的路径数、覆盖有效性和动态
  工件扫描布尔值。

## 13. 浏览器人工验收

项目负责人已于 2026-07-28 完成固定浏览器清单：

- [x] Frontend 页面：`PASSED`。
- [x] 创建 Conversation：`PASSED`。
- [x] 知识问答：`PASSED`。
- [x] 完整 Tool：`PASSED`。
- [x] `NEEDS_INPUT` + `730 °C` 补参：`PASSED AFTER FIX`。
- [x] 图片预览：`PASSED`。
- [x] 图片下载：`PASSED`。
- [x] Tool retry：`COVERED BY M10 MANUAL ACCEPTANCE + M11-B E2E`；
  本轮正式数据库无可操作失败卡片，未人为制造故障。
- [x] Explanation retry：
  `COVERED BY M10 MANUAL ACCEPTANCE + M11-B E2E`；本轮正式数据库无可操作
  失败卡片，未人为制造故障。
- [x] 刷新恢复：`PASSED`。
- [x] Timeline 卡片位置稳定：`PASSED`。

第 5 项重新验收使用全新 `NEEDS_INPUT` Task，只输入 `730 °C`；未再出现
HTTP 502，保持同一个 Task，Task 最终 `SUCCEEDED`，结构化 Result 和
Explanation 正常，Timeline 卡片位置不移动。权威结果记录
`browser_manual_scenarios=11`，人工检查没有被自动化开关替代。

浏览器第 5 项首次使用真实补参文本 `730 °C` 时稳定返回 HTTP 502 和安全的
Chat orchestration protocol error；临时改用带“完整合法 Tool 请求”的魔法词
后成功，确认缺陷位于默认 Mock 的固定关键词路由。该缺陷已通过 TDD 修复：
contract 覆盖五种受控时效温度表达，M11-A journey 和 M11-B supplement replay
均改用真实用户输入 `730 °C`。TDD 聚焦集合由 42 项中的 7 个预期失败恢复为
`42 passed`；最新 Runner 再次确认 M11 E2E `24 passed`、Backend 全量
`882 passed` 和 `failed=0`。项目负责人随后按上述真实输入完成第 5 项复测并
确认 `PASSED AFTER FIX`。

## 14. 临时资源清理

- 临时 PostgreSQL 数据库在 E2E session finally 中删除。
- 临时 MinIO bucket 先删除本轮对象再删除 bucket；预存同名资源拒绝接管。
- 每个测试 Runtime HTTP server 使用动态 loopback 端口、后台 thread 和
  有界 event；teardown 后 join 并重新 bind，证明端口释放。
- Runner 失败探针证明：自启栈失败后 state、3000/8000/8100 listeners 和
  Compose running services 均为 0。
- 既有栈探针证明：失败时 state 内容及三个 listeners 保持不变，Runner 不
  调用 stop。
- Git baseline 受控探针证明：注入错误分支时 Runner exit 1、仍写 summary，
  command records 中没有 `mock_stack_start_or_reuse`，state 不存在。
- 版本证据探针证明：六条版本/Compose command record 均 exit 0，Runner
  自启栈后取得 Backend Python 版本并在 finally 精确停止；最终 state 不存在。
- 未删除 Docker volume，正式数据库和正式 bucket 不属于测试清理目标。

## 15. 真实模型排除

- `materialsagent-zta35g` 未启动。
- 未调用真实 LLM 或真实 LangChain 网络 Provider。
- 未加载、导入或运行 `SEM/` 与模型权重。
- Runtime ready 身份为 `mock-zta35g-bundle`，CPU Mock。
- SEM 完整性：57 files，`total_size_bytes=2043071133`，
  fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。

## 16. Scope、SEM 和 Git

- `SCOPE_OK M11`。
- `SCOPE_OK M11B`。
- SEM pre/post fingerprint 相同，均为 `SEM_INTEGRITY_OK`。
- `git diff --check` 和 `git diff --cached --check` 均为 exit 0。
- 项目负责人已授权精确暂存 15 个 M11-B 变更路径；尚未授权 commit、push
  或 amend。
- 修改范围仅为扩展后的 M11-B allowlist；只修改获批的三个生产文件及回归
  测试，设计基线、依赖、migration、ToolWorkflow 和公共 API 均未修改。

## 17. 已知风险

- 本地入口依赖 Windows PowerShell 5.1、CIM/WMI 和 Docker Desktop/
  Compose，不是跨平台或生产进程管理器。
- 生产 blocker 与默认 Mock 补参缺陷均已按 TDD 修复；M11-B 已通过最终代码
  审查、项目负责人浏览器验收和阶段 1A 权威验收，但 acceptance commit 尚未
  创建，因此 M11 与阶段 1A 尚不标记 `COMPLETE`。
- M12 为 `NOT STARTED / NOT AUTHORIZED`，真实 LLM 尚未运行。
- 阶段 1B 尚未开始，真实 SEM 模型尚未加载或运行。
- start/stop 继续 fail-closed；state、Docker engine/context 或 PID ownership
  不可信时需要人工核对，而不是宽泛清理。

## 18. M12 前置条件

以下仅是前置条件，不构成 M12 授权：

- 精确暂存当前 15 个 M11-B 变更路径并完成 staged diff 审计。
- 项目负责人另行授权并创建 M11-B acceptance commit。
- commit 后根据 live Git 事实完成审计，再将 M11 与阶段 1A 标记为
  `COMPLETE`。
- 另开 M12 规划/实施指令，并继续遵守真实 Secret、Provider 和阶段 1B
  模型边界。

当前 `M12 = NOT STARTED / NOT AUTHORIZED`；真实 LLM、真实 SEM 与阶段 1B
均未运行或开始。

不得把截图二进制、完整日志、PID、临时资源名、本机用户名、绝对路径或
Secret 提交进本报告或 Git。
