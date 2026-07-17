# 材料智能体平台阶段 1 详细实施计划

> 状态：待项目负责人复核
>
> 计划日期：2026-07-17
>
> 依据：第一节、第二节、第三节、第四节 A、第四节 B 五份“已确认设计基线”
>
> 本文件只规划阶段 1，不代表已经执行任何任务；本轮不创建业务代码、数据库、环境或容器，不安装依赖、不运行模型，也不执行 git commit。

> **供后续执行 Codex 使用：** 实施时必须逐里程碑工作。每个编码闭环使用一个新的 Codex 对话；开始前重读本里程碑与五份基线，结束时执行本里程碑列出的测试、人工验收和 Git diff 检查。需要代理式执行时，使用 `superpowers:subagent-driven-development`；在同一对话批量执行时，使用 `superpowers:executing-plans`。未经项目负责人通过暂停检查点，不进入下一组里程碑。

**目标：** 在不改变五份设计基线的前提下，先用 Mock Runtime 完成可运行的平台闭环，再验证并接入真实 ZTA35G 模型，形成可复现的本地 MVP、兼容性验收记录和最小运行手册。

**架构：** 平台主体保持 Python 3.11 候选的模块化单体，Vue 3 + Vite 为最小前端，PostgreSQL 保存结构化事实，MinIO 保存图片。阶段 1A 用符合 `/internal/v1` 契约的 Mock Runtime 验证全部平台行为；阶段 1B 在独立 Python 3.8 环境中只读验证旧模型文件，随后实现真实 Runtime，并通过同一 Tool Client Adapter 边界替换 Mock。

**技术栈：** Windows 11、Conda、Python 3.11 候选、FastAPI、Pydantic、SQLAlchemy、Alembic、PostgreSQL、MinIO、LangChain 可替换边界、pytest、Docker Compose、Vue 3、Vite、TypeScript、Vitest、Python 3.8、PyTorch/Torchvision/CUDA 旧模型依赖。

## 全局约束

1. 五份已确认设计基线是实施唯一架构依据；发现直接冲突时暂停，先提交设计变更建议，不能在代码中自行改写语义。
2. 阶段 1A 不加载或运行真实模型，只使用 Mock LLM 与 Mock ZTA35G Runtime。
3. 阶段 1B 的模型环境与 Backend 环境隔离；旧 PyTorch、CUDA、Joblib、scikit-learn 依赖不得安装进 Backend 环境。
4. Docker Compose 只管理 PostgreSQL 和 MinIO，不容器化 GPU Runtime，不增加 Redis、Worker 或队列。
5. 公共三个写入执行 POST 同步、请求内执行；正常返回稳定的 `NEEDS_INPUT / SUCCEEDED / PARTIALLY_SUCCEEDED / FAILED`，不使用 `202` 或 BackgroundTask 承担可靠执行。
6. Runtime 只绑定 `127.0.0.1`，内部路径只使用 `/internal/v1`，默认要求 `X-ZTA35G-Runtime-Token`。
7. MVP 固定运行参数为 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000`；seed 每次由平台生成并记录。普通用户不能修改前三项。
8. Runtime execute 最大并发为 1、无内部队列、繁忙时立即 `RUNTIME_BUSY`；Backend 不自动重试 execute。
9. 正式 PNG 仍由 Backend/AssetService 编码；Runtime 返回 JSON + Base64 安全 NumPy `.npy`，读取必须 `allow_pickle=False`。
10. 不实现 Redis、Worker、SSE、WebSocket、正式登录、上传、多 Tool、模型选择、多 GPU、自动扩缩容或生产部署。
11. `SEM/` 在权重验证前和验证期间保持只读；不得为了兼容先重构原研究代码或覆盖权重。
12. 每个里程碑坚持测试先行或测试同步，外部调用放在数据库事务外，日志与响应不得包含密钥、完整图片 bytes、Prompt、堆栈或权重路径。
13. 每个里程碑只修改其允许范围；发现需要跨范围修改时停止并请求负责人调整计划。
14. 本计划中的提交命令只供未来实施时使用；本轮不执行。每个提交都必须在项目负责人通过对应验收后进行。

---

## 项目负责人审阅层

### 1. 阶段 1 最终要实现什么

最终交付是一套本地可运行的材料智能体 MVP：用户在 Vue 聊天界面提交知识问题或 ZTA35G 工艺请求，Backend 完成聊天编排、确定性校验、Tool 执行、图片保存、结构化结果、自然语言解释、重试、幂等和统一时间线展示。真实模型接入后，平台仍使用同一公共 API、数据模型和前端。

### 2. 为什么先用 Mock Runtime

真实模型依赖旧 Python/CUDA 组合，权重大、启动慢且兼容性尚未实测。如果平台开发一开始就依赖真实模型，数据库、API、幂等或前端问题会与模型问题混在一起。Mock Runtime 复现第四节 B 的路径、Token、响应、图片载荷和错误协议，使平台主体可以先被完整验收。

### 3. 阶段 1A 与 1B 分别做什么

- 阶段 1A（M0–M11）：建立 Backend、数据库、MinIO、公共 API、Mock LLM、Mock Runtime、时间线和最小 Vue 前端，完成不依赖真实模型的全链路。
- 阶段 1B（M12–M15）：建立独立 Python 3.8 环境，只读核验权重和依赖，运行最小真实推理，测量耗时/资源，再实现真实 Runtime 并替换 Mock。

### 4. 每个里程碑后用户能看到什么

M1 能看到健康检查；M3 能创建对话和 Task；M4 能看到知识回答或缺参追问；M5 能看到 Mock ToolRun；M6 能读取正式 PNG；M7 能看到结构化结果和解释；M8 能验证重试与幂等；M9 能读取统一时间线；M10 能在浏览器完成最小交互；M11 能演示完整 Mock 闭环；M12–M13 给出模型兼容和最小推理证据；M14–M15 用真实模型完成同一界面闭环。

### 5. 哪些步骤不依赖真实模型

M0–M11 全部不依赖真实模型。数据库、MinIO、Actor、Tool Registry、参数校验、公共 API、时间线、前端、幂等、重试、错误映射和图片编码都必须在 Mock 阶段完成。

### 6. 哪些步骤必须等待模型兼容性验证

真实 Runtime 实现、真实超时值、显存结论、权重加载处理、图像真实 dtype/shape/值域和真实 E2E 必须等待 M12/M13。M12 未确认权重可加载前，不改写 `SEM/`，也不实现“猜测兼容”的真实 Adapter 行为。

### 7. 每一步如何人工验收

每个里程碑都给出可复制的 PowerShell/HTTP 命令、预期 HTTP/业务状态、应看到的界面或日志字段以及 Git diff 范围。项目负责人不需要读完代码，只需核对可运行产物、测试摘要、响应样例、截图或兼容性记录。

### 8. 某一步失败是否影响之前完成部分

每个里程碑使用新增迁移、可替换 Port/Adapter、Mock 配置开关和独立提交。失败时回退本里程碑文件或停用新 Adapter，不覆盖旧迁移、不修改历史结果、不重写前一里程碑已验收接口。真实模型失败不会破坏 Mock 平台闭环。

### 9. 必须暂停并由项目负责人确认的节点

1. M2：Backend、PostgreSQL、MinIO 和基础持久化可启动。
2. M8：Mock Tool 的 Backend 全链路（含结果、失败、幂等和重试）通过。
3. M11：统一时间线与最小 Vue 前端符合产品预期，阶段 1A 完成。
4. M12：Python 3.8 环境与全部权重能够加载，兼容性记录可接受。
5. M13：真实最小 SEM 与性能结果在项目负责人看来合理，固定运行参数可运行。
6. M15：真实 Runtime 完整端到端通过。

### 10. 如何避免 Codex 一次修改过多

一个里程碑是一组可验收结果，不要求一次对话完成全部内部步骤。默认每个新 Codex 对话只处理一个里程碑中的一个连续文件范围，并控制为“一个后端闭环”或“一个前端闭环”，不得同时大规模改数据库、API、Runtime、前端和真实模型。若预计超过 12 个生产文件或 800 行净新增，应在同一里程碑内分成两个连续对话，并在两次之间运行局部测试和 `git diff --check`。

## 阶段划分、里程碑顺序与可运行产物

保留建议的 M0–M15 顺序，不调整。原因是依赖链天然从仓库规范、Backend 启动、持久化、业务闭环、Mock Tool、资产/结果、可靠性、时间线、前端，逐步过渡到模型环境、最小推理、真实 Runtime 和真实 E2E；提前交换任何相邻大阶段都会让验收依赖尚未成立。

| 阶段 | 里程碑 | 可运行或可检查产物 | 使用真实模型 |
|---|---|---|---|
| 1A | M0 基线与仓库准备 | 目录约定、开发命令、边界检查 | 否 |
| 1A | M1 Backend 最小启动 | `/api/v1/health/live`、安全 ready 骨架 | 否 |
| 1A | M2 PostgreSQL/MinIO 基础 | Compose 两服务、DB/MinIO 探针、Actor 基础持久化 | 否 |
| 1A | M3 Conversation/Message/Task | 创建对话、提交消息、读取 Task | 否 |
| 1A | M4 Mock CHAT_ORCHESTRATION | 知识回答、Tool 候选、NEEDS_INPUT | 否，Mock LLM |
| 1A | M5 Mock Runtime 与 ToolRun | `/internal/v1` Mock、Adapter、一次 ToolRun | 否，Mock Runtime |
| 1A | M6 Asset 闭环 | Base64 `.npy` 校验、PNG、PENDING→AVAILABLE、受控读取 | 否 |
| 1A | M7 ToolResult 与 Explanation | 结构化结果、解释成功/失败 | 否，Mock LLM |
| 1A | M8 幂等、重试和失败 | 不重复写、Tool/Explanation 重试、安全错误映射 | 否 |
| 1A | M9 统一时间线 | 稳定锚点、游标、Task 卡聚合 | 否 |
| 1A | M10 最小 Vue 前端 | 浏览器聊天、结果卡、图片、重试 | 否 |
| 1A | M11 Mock 全链路验收 | 一键启动与完整 Mock E2E 报告 | 否 |
| 1B | M12 Python 3.8 与权重加载 | 环境锁定、SHA-256、模型加载兼容记录 | 只加载，不推理 |
| 1B | M13 真实最小推理 | 单张 SEM、性能预测、耗时/显存/图像记录 | 是，首次推理 |
| 1B | M14 真实 Runtime 与 Adapter | 受 Token 保护的真实 `/internal/v1` | 是 |
| 1B | M15 真实模型端到端 | Vue→Backend→真实 Runtime→MinIO→时间线 | 是 |

## 未来文件结构锁定

以下结构用于约束后续实现，不要求本轮创建：

```text
backend/
├─ pyproject.toml
├─ alembic.ini
├─ alembic/
├─ src/materialsagent/
│  ├─ main.py
│  ├─ api/{dependencies,error_handlers,routes}/
│  ├─ application/
│  ├─ domain/{models,ports}/
│  └─ infrastructure/{config,logging,db,llm,storage,tool_clients}/
└─ tests/{unit,contract,integration,api,e2e}/
frontend/
├─ package.json
├─ vite.config.ts
├─ src/{api,components,stores,types,views}/
└─ tests/
mock-runtime/
├─ pyproject.toml
├─ src/materialsagent_mock_runtime/
└─ tests/
zta35g-runtime/
├─ requirements-win-py38.lock.txt
├─ src/materialsagent_zta35g_runtime/
├─ compat/
├─ tests/{contract,compatibility}/
└─ docs/
environments/
├─ materialsagent-backend.yml
└─ materialsagent-zta35g.yml
scripts/
├─ dev/
└─ acceptance/
docs/
├─ superpowers/{specs,plans}/
└─ acceptance/
docker-compose.yml
.env.example
```

目录按职责而不是按“大而全技术层”拆分：公共契约在 domain/ports，业务编排在 application，HTTP/数据库/MinIO/LLM/Runtime 调用在 adapters。Mock Runtime 与真实 Runtime 不进入 Backend 包。

## 测试层次与统一完成规则

| 层次 | 主要范围 | 默认命令 |
|---|---|---|
| 单元测试 | 校验、状态聚合、PNG、错误映射、游标 | `conda run -n materialsagent-backend python -m pytest backend/tests/unit -q` |
| 契约测试 | 公共 `/api/v1`、`/internal/v1`、ToolExecutionOutput | `conda run -n materialsagent-backend python -m pytest backend/tests/contract -q` |
| Repository/数据库集成 | 外键、唯一约束、短事务、并发幂等 | `conda run -n materialsagent-backend python -m pytest backend/tests/integration/db -q` |
| MinIO 集成 | put/head/get、Asset 状态、损坏对象 | `conda run -n materialsagent-backend python -m pytest backend/tests/integration/storage -q` |
| 公共 API | 状态码、所有权、响应过滤 | `conda run -n materialsagent-backend python -m pytest backend/tests/api -q` |
| Mock Runtime | Token、固定参数、`.npy`、busy、错误 | `conda run -n materialsagent-backend python -m pytest mock-runtime/tests -q` |
| 真实 Runtime 兼容 | 权重、图像、参数、耗时、显存 | `conda run -n materialsagent-zta35g python -m pytest zta35g-runtime/tests/compatibility -q -s` |
| 前端 | 类型、组件、最小交互 | `npm --prefix frontend run test -- --run` |
| Mock E2E | 公共请求到 Mock Runtime/MinIO/时间线 | `conda run -n materialsagent-backend python -m pytest backend/tests/e2e/test_mock_journey.py -q -s` |
| 真实 E2E | 公共请求到真实模型结果 | `conda run -n materialsagent-backend python -m pytest backend/tests/e2e/test_real_zta35g_journey.py -q -s` |

每个里程碑必须同时满足：列出的成功、输入错误、依赖失败、幂等/重试场景均有自动化证据；人工验收命令输出与预期一致；`git diff --check` 为零错误；`git status --short` 只出现本里程碑允许文件；项目负责人确认暂停点后才能继续。

---

## M0：阶段 0 基线与仓库准备

**目标：** 锁定五份基线、未来目录、开发命令和变更边界，确保后续每个 Codex 对话可独立审计。

**用户价值：** 项目负责人可以在任何实现开始前看清“会创建什么、不会碰什么”，避免研究模型目录或已确认文档被误改。

**前置条件：** 五份文档均为已确认设计基线；负责人已批准本计划。

**允许修改的文件范围：** `.editorconfig`、`.gitattributes`、`.env.example`、根 `README.md`、`environments/README.md`、`scripts/dev/check-scope.ps1`、`docs/acceptance/phase-1-checklist.md`。不创建业务包。

**明确不做：** 不创建 Conda 环境、Compose、Backend/前端/Runtime 代码，不改五份基线和 `SEM/`。

**实现步骤：**

1. 写入目录与命名规范、UTF-8/LF 规则和 Windows PowerShell 命令约定。
2. `.env.example` 只列变量名与安全说明：数据库、MinIO、LLM、Mock/真实 Runtime URL、共享 Token；不写真实 secret。
3. `check-scope.ps1` 读取 `git status --short`，在当前里程碑之外出现文件时以非零退出。
4. `phase-1-checklist.md` 列出六个暂停点和每个阶段禁止项。

**自动化测试与四类场景：**

- 成功：允许范围内文件被脚本接受。
- 输入错误：脚本收到未知里程碑名时退出 2 并打印 `UNKNOWN_MILESTONE`。
- 依赖失败：Git 不可用时退出 3 并打印 `GIT_UNAVAILABLE`。
- 幂等/重试：连续运行两次不修改文件且输出相同。

运行：`powershell -ExecutionPolicy Bypass -File scripts/dev/check-scope.ps1 -Milestone M0`

预期：退出码 0，输出 `SCOPE_OK M0`；第二次运行无新 diff。

**人工验收步骤：** 打开 `.env.example`，确认没有 secret；运行 `git status --short`，确认五份基线与 `SEM/` 没有本任务新增变化。

**失败时回退：** 删除本里程碑新增的规范文件；不回退或覆盖任何基线文档。

**完成证据：** 命令输出、允许范围清单、`git diff --check`、`git diff --stat`。未来建议提交：`chore: prepare phase 1 repository conventions`。

## M1：Backend 最小启动与健康检查

**目标：** 建立 Python 3.11 Backend 最小应用工厂、配置解析、结构化日志和无模型健康检查。

**用户价值：** 可启动的 API 进程证明平台骨架和本机开发环境成立，且 live 不被数据库或模型故障拖垮。

**前置条件：** M0 通过；项目负责人允许创建 Backend 环境。

**允许修改的文件范围：** `environments/materialsagent-backend.yml`、`backend/pyproject.toml`、`backend/src/materialsagent/{main.py,infrastructure/config.py,infrastructure/logging.py,api/routes/health.py}`、对应 `backend/tests/{unit,api}`。

**明确不做：** 不连接数据库/MinIO/LLM/Runtime，不创建业务表，不实现 Conversation 或 Tool。

**接口：** `create_app() -> FastAPI`；`GET /api/v1/health/live` 固定安全 LIVE；`GET /api/v1/health/ready` 返回未接入组件的安全 `NOT_READY/DEGRADED`，不探测模型。

**实现步骤：**

1. 先写 health 与配置失败测试，确认缺少必需设置时只在 ready 中安全表达。
2. 创建 Python 3.11 环境文件和 Backend 包；依赖由 `pyproject.toml` 集中声明并在环境创建后形成已安装版本记录。
3. 实现应用工厂、request_id 中间件、JSON 结构化日志和 health 路由。
4. 启动 uvicorn，验证响应不泄漏主机路径、环境变量值或堆栈。

**自动化测试与四类场景：** 成功 live=200/LIVE；输入错误对未知路径返回受控 404；依赖失败时 ready=503/NOT_READY 而 live 仍 200；重复 GET 无副作用且 request_id 各自独立。

运行：`conda env create -f environments/materialsagent-backend.yml`；`conda run -n materialsagent-backend python -m pytest backend/tests/unit backend/tests/api/test_health.py -q`

预期：环境是 Python 3.11.x；pytest 0 failed；live 响应只含 `status`、`checked_at` 和 `request_id`。

**人工验收步骤：** `conda run -n materialsagent-backend python -m uvicorn materialsagent.main:create_app --factory --app-dir backend/src --host 127.0.0.1 --port 8000`，另一个终端执行 `Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/live`。

**失败时回退：** 删除 Backend Conda 环境和 M1 新文件；M0 规范保留。

**完成证据：** Python 版本、pytest 摘要、live/ready 响应样例、日志样例、`git diff --check` 和只含 M1 文件的 diff。未来建议提交：`feat: add backend health skeleton`。

## M2：PostgreSQL、MinIO 与基础持久化

**目标：** 只用 Docker Compose 启动 PostgreSQL/MinIO，建立配置、SQLAlchemy/Alembic、Actor 基础记录、StorageService 和 ready 探针。

**用户价值：** 平台第一次拥有可恢复的结构化事实和对象存储，并能明确显示哪个依赖不可用。

**前置条件：** M1 通过。

**允许修改的文件范围：** `docker-compose.yml`、`.env.example`、`backend/alembic*`、`backend/src/materialsagent/infrastructure/{db,storage}`、`domain/models/actor.py`、`domain/ports/{unit_of_work.py,storage.py}`、`application/bootstrap.py`、M2 tests。

**明确不做：** Compose 不管理 Backend、Runtime、Redis 或 GPU；不创建 Conversation/Task/Tool 表；不跨 MinIO 开数据库长事务。

**接口：** `UnitOfWork` 只提交短 PostgreSQL 事务；`StorageService.put/head/get/delete` 不写数据库；ready 分别汇总 `postgresql` 与 `object_storage`。

**实现步骤：**

1. 写 PostgreSQL 连接失败、MinIO 连接失败、Actor 唯一性和 StorageService put/head/get/delete 测试。
2. Compose 只定义 `postgresql`、`minio` 与初始化 bucket 的受控配置；volume 名固定但不提交数据。
3. 建立 Alembic 基线和 Actor migration，Repository/UoW 用短事务。
4. 实现 MinIO Adapter 与安全错误分类；ready 的两个探针各自有短超时。

**自动化测试与四类场景：** 成功创建/读取本地匿名 Actor并 put/get 测试对象；输入错误拒绝非法 object key；依赖失败分别停止 PostgreSQL 或 MinIO 并得到 NOT_READY/DEGRADED；同 actor_id/同对象写重放不产生重复行或错误覆盖。

运行：`docker compose up -d postgresql minio`；`conda run -n materialsagent-backend alembic -c backend/alembic.ini upgrade head`；`conda run -n materialsagent-backend python -m pytest backend/tests/integration/db backend/tests/integration/storage backend/tests/api/test_health.py -q`

预期：Compose 只有两个业务依赖服务；migration 到唯一 head；0 failed；ready 不返回连接串、端口、bucket 或 secret。

**人工验收步骤：** `docker compose ps` 显示 PostgreSQL/MinIO healthy；停止 MinIO 后 ready 为 DEGRADED 或 NOT_READY 的基线映射，live 保持 200；恢复后 ready 恢复。

**失败时回退：** `docker compose down` 停服务；Alembic 只在本地空库按 downgrade 验证，禁止对已含验收数据的库破坏性回退；M1 仍可独立运行。

**完成证据：** Compose 服务清单、Alembic heads、集成测试、故障注入响应、`git diff --check`。到此必须暂停，等待负责人确认检查点 1。未来建议提交：`feat: add postgres and minio foundations`。

## M3：Conversation、Message、Task 最小闭环

**目标：** 纵向实现 Actor 所有权下的 Conversation 创建、UserMessage/Task 原子创建和 Task 查询，不接 LLM/Tool。

**用户价值：** 用户请求第一次成为可靠、可查询、不会留下半条消息的业务事实。

**前置条件：** 检查点 1 通过。

**允许修改的文件范围：** Conversation/Message/Task/TaskInputRevision domain、repositories、migration、application services、`/conversations` 与 `/tasks` 路由及 M3 tests。

**明确不做：** 不调用 LLM、Runtime 或 MinIO；不实现结果、解释、重试或时间线聚合。

**接口：** `ConversationService.create/list`；`MessageSubmissionService.prepare_submission`；`TaskQueryService.get`；创建请求在一个短事务写 Conversation（必要时）、UserMessage、Task。

**实现步骤：**

1. 先写所有权、空文本、跨 Conversation、事务回滚和标题确定性测试。
2. 增加 Conversation/Message/Task/Revision migration 与 Repository。
3. 实现 `POST/GET /api/v1/conversations`、最小 messages 提交和 `GET /api/v1/tasks/{id}`。
4. 首条 UserMessage 成功后确定性生成标题；空白回退“新对话”，失败不影响消息 Task 提交。

**自动化测试与四类场景：** 成功创建 Conversation/UserMessage/Task；输入错误为空白或跨会话目标；依赖失败在事务提交异常时三者全部不落半条；同一次 Repository 操作重放由唯一保护避免重复（完整公共幂等留 M8）。

运行：`conda run -n materialsagent-backend alembic -c backend/alembic.ini upgrade head`；`conda run -n materialsagent-backend python -m pytest backend/tests/integration/db/test_conversation_task.py backend/tests/api/test_conversations.py backend/tests/api/test_tasks.py -q`

预期：0 failed；创建 Conversation 返回 201；消息提交的临时 M3 响应只反映已持久化 Task，不伪造业务成功。

**人工验收步骤：** 用 `Invoke-RestMethod` 创建会话、提交一条消息、查询 Task；确认标题规则、UTC 时间和 Actor 404 隔离。

**失败时回退：** 回退 M3 路由与 migration 仅限本地空验收库；M2 Actor/Storage 基础保持可用。

**完成证据：** API 样例、事务故障测试、Alembic 单 head、数据库行数核对、`git diff --check`。未来建议提交：`feat: add conversation message task slice`。

## M4：Mock CHAT_ORCHESTRATION

**目标：** 实现可替换 Chat Orchestration Port、Mock LLM 与确定性单位/参数校验，覆盖知识回答、Tool 候选和 NEEDS_INPUT。

**用户价值：** 用户能收到知识回答或准确追问，完整非法输入不会误执行 Tool。

**前置条件：** M3 通过。

**允许修改的文件范围：** `domain/ports/chat_orchestration.py`、`application/{chat_orchestration,normalization,validation}.py`、`infrastructure/llm/mock.py`、LLMCall migration/repository、messages API 和 M4 tests。

**明确不做：** 不接真实 LLM，不创建 ToolRun，不运行 Mock Runtime，不让 LLM 负责最终单位转换或范围判断。

**接口：** `ChatOrchestrationPort.orchestrate(input) -> KnowledgeAnswer | ToolCandidate | NeedsInputCandidate`；Application 将候选转为正式 Message/Revision/Task 状态。

**实现步骤：**

1. 为三类判别联合、温度/时间单位、精度、范围、unsupported material、LLM 超时写失败测试。
2. 实现 Mock LLM 可按受控测试输入返回三类结果；完整 Prompt 不入库。
3. 实现 `60 min = 1 h`、四维参数和 requested_outputs 的确定性规则。
4. 在消息 POST 中同步完成编排并返回 `SUCCEEDED/NEEDS_INPUT/FAILED`；超时规范化为 `UPSTREAM_TIMEOUT`。

**自动化测试与四类场景：** 成功知识回答和合法 ToolCandidate；输入错误缺参→NEEDS_INPUT、越界→422 FAILED；依赖失败 Mock LLM 超时→Task FAILED；相同内部调用结果重复终结不创建第二 AssistantMessage/Revision。

运行：`conda run -n materialsagent-backend python -m pytest backend/tests/unit/test_normalization.py backend/tests/contract/test_chat_orchestration.py backend/tests/api/test_message_orchestration.py -q`

预期：三类 route 均有测试；0 failed；日志可见 request_id/task_id/LLMCall，但无 Prompt。

**人工验收步骤：** 分别提交知识问题、缺 aging_temperature、solution_time=180 min、越界温度；核对 Task、Revision 和追问。

**失败时回退：** 配置切回无 Orchestration 的 M3 路径，仅用于开发诊断；不得在产品验收中伪造成功。

**完成证据：** 三类响应、单位转换快照、超时响应、LLMCall 安全投影、diff 检查。未来建议提交：`feat: add mock chat orchestration`。

## M5：Mock Runtime、Tool Registry 与 ToolRun

**目标：** 建立静态 Tool Registry/Catalog、独立 Mock Runtime、HTTP Client Adapter 和一次 MaterialTool.execute/ToolRun 闭环。

**用户价值：** 在无真实模型情况下验证后端确实会调用一个符合第四节 B 的 Tool，并能区分未就绪、繁忙、超时和协议错误。

**前置条件：** M4 通过。

**允许修改的文件范围：** `mock-runtime/**`、Backend Tool Registry/Catalog、MaterialTool/ToolExecutionOutput ports、ToolRun model/repository/migration、tool_clients、tools routes、M5 tests。

**明确不做：** 不保存 Asset/ToolResult/Explanation，不加载 `SEM/`，不自动重试 execute，不建立 Runtime 队列或让 Backend 启动 Runtime。

**接口：** Mock 提供 token 保护的 live/ready/execute；Adapter 只发送三个 ID、固定 Tool/版本、四维参数、requested_outputs、seed 与 `1/2.0/1000`。

**实现步骤：**

1. 先写 Token、固定参数、版本、busy=1、超时、无自动重试和响应 ID 回显契约测试。
2. Mock Runtime 返回确定性 Base64 `.npy` 和可注入的安全失败，不访问数据库/MinIO。
3. 实现 Registry 唯一事实源和 Catalog 投影；MaterialTool 只通过 Adapter 调用 Runtime。
4. ToolRun 创建/终结使用短事务；HTTP 调用在事务外。

**自动化测试与四类场景：** 成功创建一个 ToolRun；输入错误 guide_scale=3.0 或未知 Tool 被 Runtime 拒绝；依赖失败 Runtime 未启动/超时/忙正确映射；同一 ToolRun 不自动重发，显式第二次执行必须新 ID/seed（公共端点留 M8）。

运行：`conda run -n materialsagent-backend python -m pytest mock-runtime/tests backend/tests/contract/test_runtime_contract.py backend/tests/integration/db/test_tool_run.py backend/tests/api/test_tools.py -q`

预期：0 failed；Mock ready 不推理；第二并发 execute 为 503 `RUNTIME_BUSY`、`retryable=true`；Adapter 调用计数在超时后仍为 1。

**人工验收步骤：** 手动启动 Mock Runtime，先带 Token检查 ready，再启动 Backend；提交完整 Tool 请求并查询 ToolRun；用错误 Token 验证三条内部路径均拒绝。

**失败时回退：** Backend 配置回到 Tool unavailable；M4 知识回答/NEEDS_INPUT 仍可运行。删除 Mock 不影响平台数据库。

**完成证据：** Mock 请求/响应样例、固定参数断言、busy 并发测试、ToolRun 记录、日志三 ID、diff 检查。未来建议提交：`feat: add mock zta35g runtime slice`。

## M6：Asset PENDING → AVAILABLE

**目标：** 解码并校验 Mock `.npy`，由 Backend 正式编码 PNG，完成 Asset PENDING→MinIO→AVAILABLE 与受控内容读取。

**用户价值：** 用户第一次可以可靠查看生成图片，且图片缺失或损坏时不会被误报为成功。

**前置条件：** M5 通过。

**允许修改的文件范围：** Asset model/repository/migration、`application/asset_service.py`、图片校验/PNG 编码、StorageService Adapter、assets API、M6 tests。

**明确不做：** Runtime 不编码 PNG、不上传 MinIO；不实现真实上传、签名 URL、后台 orphan 清理器。

**接口：** ImagePayload 严格 `<f4`、`[512,512]`、二维 C-order、有限值、`[-1,1]`、`base64+npy`、`allow_pickle=False`；AssetService 执行 PENDING→put→AVAILABLE。

**实现步骤：**

1. 写 NaN/Inf、object dtype、shape、dtype、Base64、4 MiB、hash、PNG 量化和 MinIO 故障测试。
2. 实现内存 `.npy` 安全解码与第二节固定 PNG 映射。
3. 实现两个短事务和 MinIO put/head；失败落 FAILED/ORPHANED 锚点。
4. 实现 Asset 元数据/content API、Actor 所有权、inline/attachment。

**自动化测试与四类场景：** 成功得到 AVAILABLE mode-L PNG；输入错误非法 `.npy`→INVALID_MODEL_OUTPUT；依赖失败 MinIO put/AVAILABLE Tx2 故障→FAILED/PENDING 可恢复；同一 asset 终结重试不重复创建对象且状态条件更新安全。

运行：`conda run -n materialsagent-backend python -m pytest backend/tests/unit/test_image_payload.py backend/tests/unit/test_png_encoder.py backend/tests/integration/storage/test_asset_lifecycle.py backend/tests/api/test_assets.py -q`

预期：0 failed；PNG 512×512、8-bit、无 alpha；非 AVAILABLE content=409；响应无 object_key。

**人工验收步骤：** 请求 Mock SEM，下载 inline PNG并用图片查看器打开；显式 attachment 检查响应头；停止 MinIO 后确认不返回成功图片。

**失败时回退：** 保留 ToolRun 诊断，把相关 Asset 标为 FAILED/ORPHANED；禁用 Asset 内容端点，不修改 M5 ToolRun 历史。

**完成证据：** PNG 元数据/SHA-256、MinIO HEAD、失败恢复记录、API 头、diff 检查。未来建议提交：`feat: add generated asset lifecycle`。

## M7：ToolResult 与 Explanation

**目标：** 在 Asset AVAILABLE 后提交 ToolResult/ResultAssetLink，并用 Mock Explanation Port 生成独立解释。

**用户价值：** 用户能同时获得结构化性能、图片和解释；解释失败时已有结果不会丢失。

**前置条件：** M6 通过。

**允许修改的文件范围：** ToolResult、ResultAssetLink、Explanation、LLMCall models/repositories/migrations，result/explanation services，tool-results routes，M7 tests。

**明确不做：** 不接真实 LLM，不复制 ToolResult/Explanation 为 Message，不跨 ToolRun 混合 Asset，不实现重试端点（M8）。

**接口：** ResultService 在一个短事务校验 ToolRun/Asset/Task 来源并提交 Result、links、selected 引用；Explanation 只读已提交 Result。

**实现步骤：**

1. 写跨 Task/ToolRun Asset、非 AVAILABLE、部分成功、仅性能失败、解释失败/超时测试。
2. 实现 ToolResult 输出集合与状态聚合，示例性能值只来自 Mock 响应。
3. 实现 Explanation prepare→事务外 Mock LLM→finalize；不保存 Prompt。
4. 消息 POST 同步等待稳定 Task 状态后返回，不使用 202。

**自动化测试与四类场景：** 成功 Result+Asset+Explanation；输入错误跨来源 Link 被拒；依赖失败 Explanation 超时→HTTP 200/Task PARTIALLY_SUCCEEDED 且 Result保留；重复 Explanation finalize 不覆盖旧事实。

运行：`conda run -n materialsagent-backend python -m pytest backend/tests/integration/db/test_result_commit.py backend/tests/contract/test_tool_execution_output.py backend/tests/api/test_tool_results.py backend/tests/api/test_explanation_outcomes.py -q`

预期：0 failed；ToolResult 成功后 Explanation 失败仍返回结构化结果；数据库无跨 ToolRun link。

**人工验收步骤：** 演示全部成功、图片成功/性能失败、只请求性能失败、解释超时四种响应。

**失败时回退：** Explanation Adapter 可切换为明确失败替身；已提交 ToolResult/Asset 不回滚，不重跑 Tool。

**完成证据：** 四类响应、来源约束测试、selected 引用、Explanation LLMCall、diff 检查。未来建议提交：`feat: add tool result and explanation slice`。

## M8：幂等、Tool/Explanation 重试与失败路径

**目标：** 完成 IdempotencyRecord、四种 operation、Tool 重试、Explanation 重试、超时/客户端断开和安全错误映射。

**用户价值：** 网络重放不会重复生成图片或解释；用户可以明确重试失败环节，旧尝试仍可查询。

**前置条件：** M7 通过。

**允许修改的文件范围：** Idempotency model/repository/migration、idempotency/retry services、tool-runs/explanations routes、error handlers、M8 tests。

**明确不做：** 不增加取消 API、Worker、BackgroundTask、execute 自动重试、成功 Task 的“重开”；不承诺断开后可靠后台继续。

**接口：** `(actor_id, operation, Idempotency-Key)` 唯一；同 digest 返回原资源当前事实，异 digest 409；Tool retry 新 tool_run_id/seed，Explanation retry 同 result_id 新 explanation/LLMCall。

**实现步骤：**

1. 写并发同 key、异 digest、断开重放、Tool busy/timeout、Explanation timeout、迟到 Runtime 响应测试。
2. 在创建 Message/Revision/ToolRun/Explanation 前原子绑定 IdempotencyRecord。
3. 实现两个重试 POST 的可重试状态校验和同步稳定响应。
4. 规范化 Runtime/LLM/DB/MinIO 错误；禁止堆栈、路径和 secret 外泄。

**自动化测试与四类场景：** 成功显式 Tool/Explanation 重试；输入错误同 key 异摘要=409或不可重试状态；依赖失败 timeout/unreachable/busy 持久化稳定失败；20 个并发同 key 只创建一个目标资源、Adapter 调用一次。

运行：`conda run -n materialsagent-backend python -m pytest backend/tests/integration/db/test_idempotency_concurrency.py backend/tests/api/test_tool_retry.py backend/tests/api/test_explanation_retry.py backend/tests/contract/test_error_mapping.py -q`

预期：0 failed；无 202；POST 正常终态不为 PENDING/RUNNING；execute 调用计数符合“无自动重试”。

**人工验收步骤：** 断开客户端后用同 key 重放；两次显式 Tool retry 核对不同 ID/seed；Explanation retry 核对 Runtime 调用计数不变。

**失败时回退：** 禁用重试端点但保留查询；不得删除旧 ToolRun/Result/Explanation。幂等 migration 不在有数据环境破坏性回退。

**完成证据：** 并发计数、资源行数、重放响应、超时/busy 映射、安全扫描、diff 检查。到此必须暂停，等待负责人确认检查点 2。未来建议提交：`feat: add idempotency and explicit retries`。

## M9：Conversation 统一时间线 API

**目标：** 组装 USER_MESSAGE、ASSISTANT_MESSAGE、TOOL_TASK 判别联合，使用稳定锚点和游标分页。

**用户价值：** 用户能在一条历史中看见知识问答和 Tool 卡，重试不会把旧卡移动或重复展示结果。

**前置条件：** 检查点 2 通过。

**允许修改的文件范围：** timeline query service/views、Conversation timeline route、查询 Repository、M9 tests。

**明确不做：** 不建 TimelineItem 表，不复制 Result/Explanation 到 Message，不实现 SSE，不让前端自行排序。

**接口：** `anchor_at=initial UserMessage.created_at`，回退 Task.created_at；顶层 `(anchor_at,type_rank,item_id)`；Task 内部按来源稳定排序。

**实现步骤：**

1. 写同时间戳排序、分页边界、Task updated、Tool/Explanation retry、历史折叠和所有权测试。
2. 实现查询投影和有签名/防篡改的不透明游标。
3. 默认返回 selected run、attempt_count、has_history；完整历史由 Task GET。
4. 限制 page size、Result data 与 input thread 大小。

**自动化测试与四类场景：** 成功混合时间线；输入错误篡改游标=受控 400/422；依赖失败查询事务失败=安全 503/500；相同游标重放结果稳定，重试后旧 anchor/分页位置不变。

运行：`conda run -n materialsagent-backend python -m pytest backend/tests/unit/test_timeline_sort.py backend/tests/integration/db/test_timeline_query.py backend/tests/api/test_timeline.py -q`

预期：0 failed；Tool初始 UserMessage不重复为顶层项；object_key/model_bundle 不出现。

**人工验收步骤：** 创建知识问答与 Tool Task，分页读取；重试 Tool/Explanation 后用旧 cursor 复查卡片位置。

**失败时回退：** 保留 Task/Result 详情 API，暂时关闭 timeline 路由；不改变底层事实。

**完成证据：** 排序夹具、分页前后快照、响应大小、API Schema、diff 检查。未来建议提交：`feat: add stable conversation timeline`。

## M10：最小 Vue 3 + Vite 前端

**目标：** 在 Mock 后端已稳定后建立最小聊天界面、Task 卡、图片、结果、NEEDS_INPUT、重试和轮询。

**用户价值：** 项目负责人无需读 JSON 即可完成主要产品验收。

**前置条件：** M9 通过；不得在 Mock 闭环前开始复杂前端。

**允许修改的文件范围：** `frontend/**`、`.env.example` 前端 API base 变量、前端 tests；仅在发现契约生成缺口时修改 Backend OpenAPI 导出脚本。

**明确不做：** 不实现登录、上传、SSE、复杂设计系统、管理后台、模型选择或多 Tool 页面。

**界面：** Conversation 列表、聊天输入、三类 TimelineItem、Tool Task 状态卡、missing fields、Result/Asset/Explanation、Tool/Explanation retry 按钮。

**实现步骤：**

1. 先写 API client 类型、Task 卡状态和幂等 key 保留测试。
2. 创建 Vue/Vite/TypeScript 最小项目，API base 只来自启动配置。
3. 实现提交时持有 Idempotency-Key，网络结果不确定时复用同 key。
4. 使用 Task/timeline 轮询最终事实；图片 inline，下载时显式 attachment。

**自动化测试与四类场景：** 成功显示知识回答和 Tool 结果；输入错误空消息/NEEDS_INPUT目标缺失在 UI 阻止或展示；依赖失败网络/Runtime/Explanation错误保留可用结果；重复点击和网络重试复用 key，不重复创建卡片。

运行：`npm --prefix frontend install`；`npm --prefix frontend run test -- --run`；`npm --prefix frontend run build`

预期：测试 0 failed；Vite build 成功；bundle 中不含 Runtime URL、Token、object_key 或 MinIO secret。

**人工验收步骤：** `npm --prefix frontend run dev -- --host 127.0.0.1`；浏览器完成知识问答、Tool、补参、两类重试、图片查看/下载和错误场景。

**失败时回退：** 前端回到上一可构建提交；Backend API/M9 时间线继续可用，禁止为前端临时绕过 API 所有权或契约。

**完成证据：** 测试/build 输出、关键页面截图、Network 响应检查、重复点击资源计数、diff 检查。未来建议提交：`feat: add minimal chat frontend`。

## M11：阶段 1A 完整 Mock 端到端验收

**目标：** 固化一键启动/停止开发依赖、Mock E2E 测试和阶段 1A 验收记录，不新增产品能力。

**用户价值：** 在完全不运行真实模型时，平台已经能稳定演示最终用户旅程，后续模型问题不会遮蔽平台问题。

**前置条件：** M10 通过。

**允许修改的文件范围：** `scripts/dev/{start,stop}-mock-stack.ps1`、`scripts/acceptance/run-phase-1a.ps1`、E2E tests、`docs/acceptance/phase-1a-report.md`。

**明确不做：** 不接真实模型、不改公共 Schema、不顺便扩展 UI/数据库。

**实现步骤：**

1. 编排 PostgreSQL/MinIO、Mock Runtime、Backend、Frontend 的显式开发启动；进程失败可清理，不作为生产守护。
2. E2E 覆盖用户消息→编排→Mock Tool→PNG→Result→Explanation→timeline/UI。
3. 故障注入覆盖 DB、MinIO、Runtime、Explanation 和客户端重放。
4. 生成不含 secret/图片 payload 的验收报告。

**自动化测试与四类场景：** 成功完整双输出；输入错误缺参/越界；依赖失败 Runtime/MinIO/Explanation；同 key 重放和两类显式重试。

运行：`powershell -ExecutionPolicy Bypass -File scripts/acceptance/run-phase-1a.ps1`

预期：脚本最后输出 `PHASE_1A_ACCEPTANCE_PASSED`，列出每类场景数量与 0 failed；不加载任何 `SEM/` 权重。

**人工验收步骤：** 项目负责人按报告中的浏览器步骤走完一次；核对五份设计的关键状态、时间线位置和错误安全性。

**失败时回退：** 停止 Mock stack，修复失败所在里程碑；不进入 M12，不改变已验收数据模型来迁就测试。

**完成证据：** 完整命令日志、E2E 报告、界面截图、资源计数、`git diff --check`。到此必须暂停，等待负责人确认检查点 3 和阶段 1A 完成。未来建议提交：`test: add phase 1a mock acceptance`。

## M12：Python 3.8 模型环境与权重加载验证

**目标：** 建立隔离的 `materialsagent-zta35g` 环境，只读核验模型文件 SHA-256，加载 DDPM、DenseNet121 和两个 SVR，形成兼容性验收记录。

**用户价值：** 在写真实 Runtime 前先证明旧模型文件能被当前硬件和依赖组合识别，失败可独立定位而不影响阶段 1A。

**前置条件：** 检查点 3 通过；负责人允许进入阶段 1B；`SEM/` 只读。

**允许修改的文件范围：** `environments/materialsagent-zta35g.yml`、`zta35g-runtime/requirements-win-py38.lock.txt`、`zta35g-runtime/compat/**`、兼容性 tests、`docs/acceptance/zta35g-compatibility.md`。只读访问 `SEM/`。

**明确不做：** 不修改/格式化/复制覆盖 `SEM/` 文件，不运行完整推理，不实现 HTTP Runtime，不把旧依赖装进 Backend。

**实现步骤：**

1. 创建 Python 3.8 环境并记录 `conda list`、Python/GPU/驱动信息。
2. PowerShell `Get-FileHash -Algorithm SHA256` 记录四个权重身份，bundle id 固定 `zta35g-sem-original-bundle`。
3. 编写只读 load probe，分别加载 DDPM、DenseNet121、两个 SVR；记录 missing/unexpected keys 和兼容转换。
4. 每加载一项立即释放临时对象；记录启动/加载耗时与峰值内存/显存，不进行 DDPM 采样。

**自动化测试与四类场景：** 成功四权重加载；输入错误 hash/文件缺失得到明确失败；依赖失败 CUDA/库版本不兼容安全记录；重复 load probe 不改权重且 SHA-256 前后相同。

运行：`conda env create -f environments/materialsagent-zta35g.yml`；`conda run -n materialsagent-zta35g python -m pytest zta35g-runtime/tests/compatibility/test_model_loading.py -q -s`

预期：Python 3.8.x；四个 hash 已记录；测试给出每个模型 PASS/FAIL、missing/unexpected keys，不把 traceback 写进公共响应。

**人工验收步骤：** 项目负责人查看兼容性表和每个权重结论；确认 `git status --short -- SEM/` 与开始前完全一致。

**失败时回退：** 删除独立 Conda 环境和 probe 生成的临时缓存；保留失败报告；不得修改权重或阶段 1A Backend。

**完成证据：** 环境导出、四个 SHA-256、加载日志安全摘要、显存记录、SEM 哈希前后对比、diff 检查。到此必须暂停，等待负责人确认检查点 4。未来建议提交：`test: verify zta35g model loading compatibility`。

## M13：真实模型最小推理与测量

**目标：** 用固定配置执行一条最小 SEM 生成和完整性能预测，验证图像/数值、耗时、显存、并发 1 和 Base64 `.npy`。

**用户价值：** 证明模型不仅能加载，还能在当前 RTX 4060 Laptop 8GB 上产生可检查结果，并为 Runtime 超时提供数据。

**前置条件：** 检查点 4 通过。

**允许修改的文件范围：** `zta35g-runtime/compat/**`、compatibility tests、`docs/acceptance/zta35g-inference.md`、受控输出目录的 `.gitignore`。`SEM/` 只读。

**明确不做：** 不实现 HTTP 服务，不调整固定参数，不重构模型，不宣称单样本基准等于生产 SLA。

**固定输入：** 使用基线范围内一组明确四维参数，seed 固定为验收记录值；`num_samples=1`、`guide_scale=2.0`、`timesteps=1000`。

**实现步骤：**

1. 先验证固定配置能否运行；若源码要求不同，停止并先更新兼容性记录/设计基线，不静默换值。
2. 运行单张 SEM，检查 float32、`[512,512]`、二维、有限值、实际 min/max。
3. 用同一原始图像运行 DenseNet+两个 SVR，记录性能结构和安全 warning。
4. 重复 warm 运行以测启动、P50/P95/P99、内存/显存；执行两个并发探针验证只允许 1。
5. 编码/解码 Base64 `.npy`，验证 `allow_pickle=False`、大小与 4 MiB 余量。

**自动化测试与四类场景：** 成功完整推理；输入错误越界/固定参数不一致在模型前拒绝；依赖失败 OOM/CUDA错误安全记录；相同 seed 重跑记录可复现程度，不把重试当成同 ToolRun。

运行：`conda run -n materialsagent-zta35g python -m pytest zta35g-runtime/tests/compatibility/test_minimal_inference.py zta35g-runtime/tests/compatibility/test_payload_and_resources.py -q -s`

预期：测试明确报告固定配置是否 PASS；图像无 NaN/Inf；`.npy` 小于响应上限；P50/P95/P99 和显存有实际数值，不预设秒数。

**人工验收步骤：** 查看由 Backend 同规则离线编码的验收 PNG和性能结果；项目负责人判断结果是否合理并审阅 warning。

**失败时回退：** 保留失败证据并停止 M14；删除受控临时输出，不改 `SEM/`。若固定配置不兼容，走设计变更门而非代码绕过。

**完成证据：** 最小推理记录、图片元数据、性能值、时间分位数、显存/内存、Base64 大小、SEM 哈希前后对比、diff 检查。到此必须暂停，等待负责人确认检查点 5。未来建议提交：`test: validate zta35g minimal inference`。

## M14：真实 Runtime 与 Local Tool Client Adapter

**目标：** 在 Python 3.8 环境实现只监听 loopback、共享 Token 保护、加载一次复用、并发 1 的真实 `/internal/v1` Runtime，并让 Backend Adapter 可配置切换 Mock/真实。

**用户价值：** 平台第一次通过正式内部契约调用真实模型，但公共 API、数据模型和前端不改变。

**前置条件：** 检查点 5 通过；M13 已给出真实超时与资源数据。

**允许修改的文件范围：** `zta35g-runtime/src/**`、Runtime contract tests、Backend `infrastructure/tool_clients/local_zta35g.py` 与配置、M14 integration tests、`docs/acceptance/zta35g-runtime-runbook.md`。

**明确不做：** Backend 不启动/重启/守护 Runtime；Runtime 不访问 DB/MinIO/LangChain，不自动重试/排队/取消，不返回 PNG。

**接口：** live/ready/execute 全部要求 `X-ZTA35G-Runtime-Token`；模型启动加载一次；固定参数严格核对；busy 立即 503 + retryable true；响应 Base64 `.npy`。

**实现步骤：**

1. 先用 Mock contract suite 对真实 Runtime 做黑盒红测。
2. 实现配置/Token安全比较、生命周期、健康状态、单执行槽和安全错误。
3. 接入 M12/M13 已验证加载/推理函数，不改变公共模型行为。
4. 实现 Backend Local Adapter 的 ID/版本/大小/`.npy`/错误校验；execute 不自动重试。
5. 按“手动 Runtime→ready→Backend”写最小运行手册。

**自动化测试与四类场景：** 成功真实 execute；输入错误 Token/参数/version/payload 被拒；依赖失败进程崩溃/超时/模型加载失败映射；公共同 key 重放不再次调用，显式 retry 新 ID/seed。

运行：`conda run -n materialsagent-zta35g python -m pytest zta35g-runtime/tests/contract -q -s`；`conda run -n materialsagent-backend python -m pytest backend/tests/contract/test_real_runtime_adapter.py -q -s`

预期：两套契约测试 0 failed；端口只在 127.0.0.1；logs 无 Token/图片 payload/权重路径；execute 调用无隐式重试。

**人工验收步骤：** 手动启动 Runtime，带 Token检查 ready，再启动 Backend；错误 Token、第二并发、超时和正常请求各执行一次。

**失败时回退：** Backend 配置切回 Mock Adapter；停止真实 Runtime；阶段 1A 全链路保持可用，真实模型文件不改动。

**完成证据：** 监听地址、ready 响应、Token负测、busy并发、超时映射、模型加载一次日志、Adapter契约结果、diff 检查。未来建议提交：`feat: add real local zta35g runtime adapter`。

## M15：真实模型完整端到端验收

**目标：** 用真实 Runtime 替换 Mock，完成公共消息提交到 Vue 时间线的真实模型全链路和最终运行手册。

**用户价值：** 项目负责人能够从真实用户入口得到正式 PNG、结构化性能和解释，并确认失败/重试/幂等仍符合产品设计。

**前置条件：** M14 通过。

**允许修改的文件范围：** real E2E tests、`scripts/acceptance/run-phase-1b.ps1`、`docs/acceptance/phase-1b-report.md`、最小运行手册；只允许为发现的契约实现缺陷修改明确所属模块。

**明确不做：** 不扩展新功能，不优化成远程 GPU，不加入自动启动/队列/SSE/登录/多 Tool，不修改 `SEM/`。

**实现步骤：**

1. 按手册手动启动 Runtime→ready→Backend→Frontend。
2. 完成三种 requested_outputs、NEEDS_INPUT、部分成功、解释超时、Tool retry、Explanation retry、断开重放。
3. 核对 Asset PNG、ToolResult provenance、timeline anchor、日志三 ID 和公共安全过滤。
4. 汇总启动、warm推理、P50/P95/P99、显存/内存和已知限制，形成最终兼容性/运行记录。

**自动化测试与四类场景：** 成功三种真实输出；输入错误缺参/越界/固定参数不可调；依赖失败 Runtime busy/timeout/crash、MinIO/Explanation失败；同 key重放与两类显式重试资源计数正确。

运行：`powershell -ExecutionPolicy Bypass -File scripts/acceptance/run-phase-1b.ps1`

预期：脚本输出 `PHASE_1B_ACCEPTANCE_PASSED`；所有契约/E2E 0 failed；报告列出真实性能与资源数据，而不是“测试通过”四个字。

**人工验收步骤：** 项目负责人在浏览器完成真实请求、查看/下载图片、比较重试、检查旧卡位置，并审阅兼容性和运行手册。

**失败时回退：** 切回 Mock Adapter，保留真实失败 ToolRun/日志/报告；不覆盖旧成功结果、不修改权重、不进入生产部署。

**完成证据：** 真实 E2E 命令日志、API/界面截图、Asset SHA-256、Runtime/Backend关联日志、性能/资源表、错误/重试资源计数、五份基线一致性检查、`git diff --check`。到此必须暂停，等待负责人确认检查点 6。未来建议提交：`test: complete phase 1 real model acceptance`。

---

## 每个未来执行对话的固定收尾模板

每个里程碑或里程碑内拆分对话在结束前必须运行并汇报：

```powershell
git status --short --branch
git diff --check
git diff --stat
git diff --name-only
```

随后运行该里程碑列出的局部测试。若修改 Backend 公共契约，还要运行全部 `backend/tests/contract`；若修改迁移，运行 `alembic upgrade head` 并检查单一 head；若修改 Runtime 契约，同时运行 Mock 与真实 Runtime contract suite；若修改前端 API 类型，运行前端 test 和 build。

汇报必须包含：目标是否达到、四类场景实际结果、命令与退出码、人工验收证据、允许范围之外是否有 diff、回退是否可用、是否到达暂停点。不得只写“测试通过”。

## 计划结束边界

本计划形成后仍处于设计与计划阶段，状态为“待项目负责人复核”。在负责人明确批准前，不执行 M0，不创建任何业务代码/环境/数据库/容器，不安装依赖，不运行模型，不修改 `SEM/`，不执行 git commit。
