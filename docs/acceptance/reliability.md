# 最小完整容错验收

验收日期：2026-10-08。此版本沿用当前 LangChain/LangGraph 与单 Backend 进程，没有新增数据库表或状态枚举，但通过 `0026_managed_outcome_unknown` 更新了数据库约束。下文分阶段保留验收证据，不代表每次文档核查都重新执行了全量测试；最近核查见末节。

## 实现范围

- 模型：首次请求加最多两次白名单重试，逻辑调用时间窗口、逐请求预算预留、失败流关闭、Retry-After 暂停、取消与原 Run 恢复。
- 工具：仅 ML 数据概况和训练状态查询自动重试；写操作和 GPU 计算结果未知时核查原 Invocation，不重新派发。完整且一致的回执才能提交结果。
- 持久化：原身份核查后有限重试纯写入，答案与成功状态原子提交；原对象键核查上传；5 秒扫描修复失去推进者的遗留 Run。
- 聊天界面：重试过程、暂停原因、继续、核查与结束操作，预算和最早恢复时间的前后端校验。

## 验证方式

Backend 使用 `D:\ProgramData\Anaconda3\envs\materialsagent-backend\python.exe`（Python 3.11）。API、数据库和存储用例使用真实本机 PostgreSQL/MinIO 的随机隔离测试库与对象；故障在提交/读取边界注入，没有停止共享数据库或对象存储。

```powershell
& 'D:\ProgramData\Anaconda3\envs\materialsagent-backend\python.exe' -m pytest backend/tests -q
& 'D:\ProgramData\Anaconda3\envs\materialsagent-backend\python.exe' -m pytest mock-runtime/tests -q
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

核心用例见 `backend/tests/unit/test_agent_reliability.py`、`backend/tests/api/test_agent_recovery_api.py`、`backend/tests/unit/test_mcp_executor.py`、`backend/tests/integration/storage/test_minio_storage.py` 和 `frontend/tests/components/agent-recovery.test.ts`。

首次 Backend 全量 1013 个测试通过（280.50 秒），包含真实 PostgreSQL/MinIO 回归；后续审查修复后的结果见下文。前端全量 94 个测试、独立 typecheck 和 build 通过；Mock Runtime 18 个测试通过。`git diff --check` 通过。构建保留既有的单个 JS chunk 超过 500 kB 提示，构建成功。

ML MCP 验收使用独立 `services/materials_ml/.venv` 与真实 HTTP/数据库/对象存储，Backend 子进程使用上述 Python 3.11：

```powershell
$env:ML_INTEGRATION='1'
$env:P4_BACKEND_PYTHON='D:\ProgramData\Anaconda3\envs\materialsagent-backend\python.exe'
& 'services/materials_ml/.venv/Scripts/python.exe' -m pytest services/materials_ml/tests/service/test_platform_mcp.py services/materials_ml/tests/service/test_mcp.py -k 'pool_releases or same_session or commit_uncertainty or unknown_training' -q
```

上述 MCP 选择共 7 个用例通过。覆盖真实会话池/作用域隔离、服务重启、训练请求超时、Backend 进程被终止后重启、工具配置停用再恢复、本地提交前失败与提交后回包丢失。核查取得完整训练提交回执后恢复原 Run，远端训练记录保持一条。验收中发现并修复异步核查路由调用同步资源登记造成的嵌套事件循环错误。

## ML Worker 补充验收

完整验收要求本机 Worker 单实例锁空闲，Backend 和 ML 分别使用独立 Python 3.11 环境，并保持 PostgreSQL/MinIO 健康。依赖容器已有配置时，可按下面命令启动并执行：

```powershell
docker compose start --wait postgresql minio
$env:P4_BACKEND_PYTHON='D:\ProgramData\Anaconda3\envs\materialsagent-backend\python.exe'
& ./services/materials_ml/scripts/acceptance.ps1
```

入口显式启用真实集成测试，运行独立 ML Engine、共享存储引用、Service、Worker、Prediction 及平台 MCP/资源链路；临时 Worker 与训练/预测子进程在各用例结束时回收。真实 Provider 与人工监督浏览器用例需要另外显式启用。

2026-10-08 补充执行时，本机 Worker 锁已空闲。首先发现 PostgreSQL/MinIO 容器已停止，启动原容器并确认健康后重跑；未重建或清空共享卷。Backend 与 ML 的 Python 3.11、依赖一致性和安装隔离检查均通过。

全量执行结果为 **206 通过、2 失败、3 跳过**（957.01 秒）。两处失败均为旧验收断言：仍将结果未知期望为 `TERMINATED` 或失败 Observation。本次仅同步两处测试为暂停待核查语义，业务实现未改动，并补强以下验证：

- 完整原回执核查后新增成功 Observation，核查本身不推进模型；人工继续原 Run 后工具执行次数仍为一次。
- 取得训练提交回执不等于训练完成；暂停 Run 和远端未完成训练仍阻止删除，显式结束 Run 并确认远端取消后才可删除会话。

随后串行定向复验 **3 个用例全部通过**（77.13 秒），覆盖上述两个失败用例及同组资源登记恢复用例。此次验收的 **208 个自动用例均已有通过结果**；没有将首轮全量执行记为一次全绿运行。复验命令：

```powershell
$env:ML_INTEGRATION='1'
$env:P4_BACKEND_PYTHON='D:\ProgramData\Anaconda3\envs\materialsagent-backend\python.exe'
& 'services/materials_ml/.venv/Scripts/python.exe' -m pytest services/materials_ml/tests/service/test_platform_resource_context.py services/materials_ml/tests/service/test_platform_resources.py -k 'registration_recovery or unknown_requires_terminal' -q
```

实际通过的 Worker 链路包含 LR/RF 训练、模型下载与预测数值往返、平台经 MCP 提交/确认/查询/预测、取消和超时、TCP 断连、Worker/Service/Backend 强制退出及重启、旧 claim 拒绝、领取/启动/完成回包丢失、原身份复用、模型/预测原子发布和进程树回收。三处跳过分别为两个真实 Provider 用例与一个人工监督浏览器用例；另有 Starlette TestClient 对 AnyIO 别名的弃用提示。

## 浏览器验收

使用真实 Vue 界面、Backend、PostgreSQL 与持久化 checkpoint，模型在独立测试进程中注入故障，不调用真实 Provider。测试端口为 5179/8765，验收结束后关闭测试进程并删除隔离数据库。

| 场景 | 观察结果 |
| --- | --- |
| 三次模型超时后暂停，点击继续 | 第四次请求完成原 Run，只显示一份有效答案 |
| 等待模型期间点击停止 | 模型只请求一次，没有后续重试或答案 |
| 执行期间浏览器断网，再恢复网络 | 后端完成后界面恢复同一结果；核查原提交后无重复请求 |

失败分片与迟到分片隔离、退避期间停止、Retry-After 倒计时、重复和过期恢复请求分别由单元、组件及 API 测试覆盖。

## 覆盖边界

本轮未调用真实模型 Provider，未运行真实 SEM/EBSD GPU 计算。数据库短暂不可达和回包丢失通过真实持久化边界故障注入验证，未对共享 PostgreSQL/MinIO 进行破坏性停机。

完整 LR/RF Worker 训练、预测及进程树回收已在上述补充验收中覆盖。此证据来自合成数据与真实 CPU Worker、HTTP、PostgreSQL/MinIO；平台模型使用确定性替身，不表示真实 Provider 的语义决策或材料科学准确性已验证。

自动换模型、熔断、任务队列和 GPU 远程取消接口仍不在首版范围。未知远端操作若无查询能力，保持待核查；历史终止任务不追溯恢复。

## 审查修复

2026-10-08 修复两个未被原断言覆盖的问题：

- 新增 `0026_managed_outcome_unknown`，同步 ORM 和 PostgreSQL 对 Managed Invocation 未知结果的约束。已有未决 Managed 记录时拒绝降级，不改写原执行事实。
- Runtime 将提交、回复、确认、重试、重新生成及人工恢复的待运行状态写入和任务登记与恢复扫描互斥；模型和工具执行在锁外进行，避免扫描快照遗漏新推进者。

新增并发回归使用隔离 PostgreSQL，分别在扫描线程等待时发起新提交和人工恢复，验证最终答案正常保存。EBSD 回归现在同时检查持久化 Invocation 状态、原操作核查和迁移降级保护，而不只检查 AgentRun 的公开状态。

修复后 Backend 全量 **1015 项通过**（274.17 秒），包含真实 PostgreSQL/MinIO、迁移往返及新增并发回归；单独执行迁移测试 **21 项通过**。Mock Runtime **18 项通过**，`git diff --check` 通过。本轮未修改前端，也未运行真实 Provider/GPU 或浏览器验收。

启用修复需升级 Backend 数据库到 `0026_managed_outcome_unknown` 并重启后端。上述自动迁移验证作用于随机隔离测试库。

2026-10-08 17:22（Asia/Shanghai）已将本机 `materialsagent` 业务库从 `0025_agent_process_stream` 升级到 `0026_managed_outcome_unknown`，确认实际 PostgreSQL 约束允许 Managed Invocation 保存未知结果，并单独重启常驻 Backend。新后端健康检查为 `READY`，PostgreSQL/对象存储及全部 7 个工具均为 `AVAILABLE`，前端代理检查通过。保留原真实 Runtime、Materials ML Service/Worker 和 Provider 配置，启动器状态已更新为 `running`；本次部署验证未调用真实 Provider 或执行 GPU 推理。

## 文档收尾复核（2026-10-08）

核查基线为 `main` / `d5ab708` 加已有未提交容错改动；这批改动尚未提交，不能以该提交号单独标识运行中的容错版本。本轮仅同步 README、流式设计和验收文档中的恢复说明与历史范围，保留既有代码及验收记录。

- 使用 Backend Python 3.11 定向运行 `test_agent_reliability.py`、`test_mcp_executor.py`，**54 项通过**（4.42 秒）。未重跑上文全量、真实 Provider/GPU、ML Worker 或浏览器验收。
- 只读核查代码迁移 head 与业务库版本均为 `0026_managed_outcome_unknown`，实际 PostgreSQL 约束允许 Managed Invocation 的 `OUTCOME_UNKNOWN`；未执行迁移或重启。
- Backend ready 为 `READY`，数据库与对象存储为 `AVAILABLE`，7 个启用工具均为 `AVAILABLE`；前端首页、前端代理 ready 与 ML Service ready 均返回 HTTP 200。启动记录为 `running` / Real Runtime / Provider。上述结果证明现场就绪，不证明本轮实际模型调用、GPU 推理或训练成功。
- 项目 12 份 Markdown 的相对文件链接与标题锚点检查通过，`git diff --check` 通过。根规则 `AGENTS.md` 无需修改；生成记忆只读。临时目录保留复核证据与运行状态，本轮未删除文件、分支或 worktree。
