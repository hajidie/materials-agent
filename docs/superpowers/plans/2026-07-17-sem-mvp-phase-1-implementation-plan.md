# 材料智能体平台阶段 1 详细实施计划

> 状态：已确认实施计划
>
> 计划日期：2026-07-17
>
> 依据：第一节、第二节、第三节、第四节 A、第四节 B 五份“已确认设计基线”
>
> 本文件规划阶段 1 的完整实施顺序和里程碑边界。当前实际执行位置、branch、HEAD、工作单元和暂停状态，只以 `docs/progress/phase-1-current-status.md` 为准。

> **供后续执行 Codex 使用：** 一个独立、可验收的工作单元约等于一个新的 Codex 对话；一个完整里程碑可以拆成多个连续对话。每个对话按本计划的固定恢复顺序读取规则、Git 状态、动态进度、当前里程碑、相关基线及代码测试，结束时执行局部测试、人工验收和 Git diff 检查。未经项目负责人通过暂停检查点，不进入下一工作单元或下一里程碑。

**目标：** 在不改变五份设计基线的前提下，先用 Mock LLM 与 Mock Runtime 完成可运行的平台闭环，再接入真实 LangChain/LLM Provider，最后验证并接入真实 ZTA35G Runtime，形成可复现的本地 MVP、兼容性验收记录和最小运行手册。

**架构：** 平台主体保持 Python 3.11 候选的模块化单体，Vue 3 + Vite 为最小前端，PostgreSQL 保存结构化事实，MinIO 保存图片。阶段 1A 用 Mock LLM 和符合 `/internal/v1` 契约的 Mock Runtime 验证全部平台行为；M12 在不改变 Application 确定性校验与 Tool 执行边界的前提下接入真实 LangChain/LLM Provider；M13–M16 在独立 Python 3.8 环境中只读验证旧模型文件，随后实现真实 Runtime，并通过同一 Tool Client Adapter 边界替换 Mock。

**技术栈：** Windows 11、Conda、Python 3.11 候选、FastAPI、Pydantic、SQLAlchemy、Alembic、PostgreSQL、MinIO、LangChain Mock/真实 Adapter、pytest、Docker Compose、Vue 3、Vite、TypeScript、Vitest、Python 3.8、PyTorch/Torchvision/CUDA 旧模型依赖。

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
14. 本计划中的提交命令只有在项目负责人通过对应验收后才能执行。未经批准不得暂存、提交或进入下一工作单元。
15. G0 必须先由项目负责人验收；只有 G0 获批、按批准范围提交并恢复干净工作区后，才能开始 M0。
16. Mock LLM 必须长期保留为自动化测试替身；真实 LLM 只通过 LangChain Adapter 和 Explanation Adapter 接入，不得直接执行 Tool。
17. API Key 只来自环境变量；不得保存完整 Prompt、Secret 或 Provider 原始响应，只保存基线允许的模板身份、digest、usage、安全结构化摘要和安全错误。
18. 一个独立、可验收的工作单元约等于一个新的 Codex 对话，不强制一个完整里程碑只使用一个对话。预计超过约 12 个生产文件、涉及两个明显独立闭环或上下文开始混淆时，必须在同一里程碑内拆成多个连续对话。
19. 每个工作单元结束前必须运行局部测试和 `git diff --check`，汇报 Git diff，更新 `docs/progress/phase-1-current-status.md`，并停在项目负责人验收点；不得擅自进入下一工作单元或 commit。

## 依赖与版本的唯一事实来源

### Backend

`backend/pyproject.toml` 是 Backend Python 包依赖和版本约束的唯一事实来源。`environments/materialsagent-backend.yml` 只声明 Conda 环境名称、Python 版本、pip 和少量确需 Conda 管理的系统级依赖；FastAPI、Pydantic、SQLAlchemy、LangChain 等 Python 包版本不得同时在两处维护。

### Frontend

Frontend 必须固定 Node.js 版本、提交 lockfile，并使用已锁定依赖；不得使用未锁定的浮动依赖。除测试外，每个会影响前端构建的工作单元必须执行 production build。

### Docker

PostgreSQL 和 MinIO 镜像必须使用明确版本标签，禁止使用 `latest`。

### ZTA35G Runtime

验证前的 Python、PyTorch、Torchvision、CUDA、NumPy、Joblib、scikit-learn 等版本只能标记为候选。M13/M14 验证成功后，必须从实际可运行环境生成精确依赖记录和兼容性证据，不能把候选版本冒充已验证锁定版本。

## 跨 Codex 对话恢复与收尾

每个新对话开始时依次读取：

1. 根 `AGENTS.md`；
2. 当前 Git branch、HEAD 和工作区；
3. `docs/progress/phase-1-current-status.md`；
4. 当前里程碑对应的计划章节；
5. 与当前任务直接相关的设计基线；
6. 当前相关代码和测试。

每个对话结束时必须：

1. 运行本工作单元测试；
2. 运行 `git diff --check`；
3. 汇报 Git diff；
4. 更新 `phase-1-current-status.md`；
5. 不擅自进入下一个工作单元；
6. 未经项目负责人验收不得 commit。

---

## 项目负责人审阅层

### 1. 阶段 1 最终要实现什么

最终交付是一套本地可运行的材料智能体 MVP：用户在 Vue 聊天界面提交知识问题或 ZTA35G 工艺请求，Backend 完成聊天编排、确定性校验、Tool 执行、图片保存、结构化结果、自然语言解释、重试、幂等和统一时间线展示。真实模型接入后，平台仍使用同一公共 API、数据模型和前端。

### 2. 为什么先用 Mock Runtime

真实模型依赖旧 Python/CUDA 组合，权重大、启动慢且兼容性尚未实测。如果平台开发一开始就依赖真实模型，数据库、API、幂等或前端问题会与模型问题混在一起。Mock Runtime 复现第四节 B 的路径、Token、响应、图片载荷和错误协议，使平台主体可以先被完整验收。

### 3. 阶段 1A 与 1B 分别做什么

- G0：完成阶段 0 收尾、长期规则、动态交接、`.codex/` 策略和 `SEM/` 完整性基线；获批并形成干净工作区后才进入 M0。
- 阶段 1A（M0–M11）：建立 Backend、数据库、MinIO、公共 API、Mock LLM、Mock Runtime、时间线和最小 Vue 前端，完成不依赖真实模型的全链路。
- 真实能力接入（M12–M16）：M12 先用真实 LangChain/LLM Provider 替换运行时 Mock LLM，同时保留 Mock 测试替身；M13–M16 再建立独立 Python 3.8 环境，只读核验权重和依赖，运行最小真实推理，实现真实 Runtime，最终同时使用真实 LLM 和真实 Runtime 完成 E2E。

### 4. 每个里程碑后用户能看到什么

G0 能看到可解释的交接与完整性基线；M1 能看到健康检查；M3 能创建对话和 Task；M4 能看到知识回答、缺参追问、硬校验错误，以及完整合法 ToolCandidate 的安全 `TOOL_UNAVAILABLE` 响应；M5 能验收 Mock Runtime、ToolRun 和内部 Tool 执行，但消息 POST 尚不返回完整 Tool 结果；M6 能通过独立 Asset 路径读取正式 PNG，但消息 POST 仍不宣称 Tool 结果完成；M7 才能通过消息 POST 看到结构化结果、图片和解释；M8 能验证重试与幂等；M9 能读取统一时间线；M10 能在浏览器完成最小交互；M11 能演示完整 Mock 闭环；M12 能验证真实 LLM 的知识回答、Tool 路由、追问和 Explanation；M13–M14 给出模型兼容和最小推理证据；M15–M16 用真实 Runtime 和真实 LLM 完成同一界面闭环。

### 5. 哪些步骤不依赖真实模型

M0–M11 全部不依赖真实模型。数据库、MinIO、Actor、Tool Registry、参数校验、公共 API、时间线、前端、幂等、重试、错误映射和图片编码都必须在 Mock 阶段完成。

### 6. 哪些步骤必须等待模型兼容性验证

真实 Runtime 实现、真实超时值、显存结论、权重加载处理、图像真实 dtype/shape/值域和真实 E2E 必须等待 M13/M14。M13 未确认权重可加载前，不改写 `SEM/`，也不实现“猜测兼容”的真实 Runtime Adapter 行为。

### 7. 每一步如何人工验收

每个里程碑都给出可复制的 PowerShell/HTTP 命令、预期 HTTP/业务状态、应看到的界面或日志字段以及 Git diff 范围。项目负责人不需要读完代码，只需核对可运行产物、测试摘要、响应样例、截图或兼容性记录。

### 8. 某一步失败是否影响之前完成部分

每个里程碑使用新增迁移、可替换 Port/Adapter、Mock 配置开关和独立提交。失败时回退本里程碑文件或停用新 Adapter，不覆盖旧迁移、不修改历史结果、不重写前一里程碑已验收接口。真实模型失败不会破坏 Mock 平台闭环。

### 9. 必须暂停并由项目负责人确认的节点

1. G0：计划修订、根规则、动态进度、忽略策略、`.codex/` 处理和 SEM 完整性基线可接受；获批后才允许提交 G0，提交并恢复干净工作区后才开始 M0。
2. M2：Backend、PostgreSQL、MinIO 和基础持久化可启动。
3. M4：五类 CHAT_ORCHESTRATION 路径可接受；完整合法 ToolCandidate 固定形成 Revision、LLMCall SUCCEEDED、Task TOOL_EXECUTION/FAILED、`TOOL_UNAVAILABLE` 和 HTTP 503，不创建 ToolRun；获批后才开始 M5。
4. M7：ToolRun→Asset→ToolResult/ResultAssetLink→Explanation→selected references→Task 稳定状态的完整链路和消息 POST 激活点可接受；获批后才开始 M8。
5. M8：Mock Tool 的 Backend 全链路（含结果、失败、幂等和重试）通过。
6. M11：统一时间线与最小 Vue 前端符合产品预期，阶段 1A 完成。
7. M12：真实 LangChain/LLM Provider 的三类编排、四维参数/单位/requested_outputs 提取、Explanation 和安全失败路径可接受。
8. M13：Python 3.8 环境与全部权重能够加载，实际精确依赖记录和兼容性证据可接受。
9. M14：真实最小 SEM 与性能结果在项目负责人看来合理，固定运行参数和真实性能记录可接受。
10. M16：真实 LLM 与真实 ZTA35G Runtime 同时启用的完整端到端通过。

### 10. 如何避免 Codex 一次修改过多

一个里程碑是一组可验收结果，不要求一次对话完成全部内部步骤。默认每个新 Codex 对话只处理一个里程碑中的一个连续文件范围，并控制为一个独立闭环，不得同时大规模改数据库、API、Runtime、前端和真实模型。若预计超过约 12 个生产文件、涉及两个明显独立闭环或上下文开始混淆，应在同一里程碑内拆成多个连续对话，并在每次结束时运行局部测试、`git diff --check`、汇报 diff 并更新动态进度文件。

## 阶段划分、里程碑顺序与可运行产物

采用 G0、M0–M16 的顺序。依赖链从交接基线、仓库准备、Backend 启动、持久化、业务闭环、Mock Tool、资产/结果、可靠性、时间线、前端与 Mock E2E，过渡到真实 LLM，再进入模型环境、最小推理、真实 Runtime 和真实 LLM + 真实 Runtime E2E；提前交换大阶段会让验收依赖尚未成立。

| 阶段 | 里程碑 | 可运行或可检查产物 | 使用真实模型 |
|---|---|---|---|
| 实施前 | G0 阶段 0 收尾与交接基线 | 长期规则、动态交接、忽略策略、SEM manifest/完整性检查 | 否 |
| 1A | M0 仓库准备 | 目录约定、开发命令、边界检查 | 否 |
| 1A | M1 Backend 最小启动 | `/api/v1/health/live`、安全 ready 骨架 | 否 |
| 1A | M2 PostgreSQL/MinIO 基础 | Compose 两服务、DB/MinIO 探针、Actor 基础持久化 | 否 |
| 1A | M3 Conversation/Message/Task | 创建对话、提交消息、读取 Task | 否 |
| 1A | M4 Mock CHAT_ORCHESTRATION | 知识回答、NEEDS_INPUT、硬校验失败；完整合法候选安全返回 `TOOL_UNAVAILABLE`，无 ToolRun | 否，Mock LLM |
| 1A | M5 Mock Runtime 与 ToolRun | Registry/Catalog、`/internal/v1` Mock、Adapter、MaterialTool、内部 ToolRun 执行；不宣称公共结果完成 | 否，Mock Runtime |
| 1A | M6 Asset 闭环 | Base64 `.npy` 校验、PNG、PENDING→AVAILABLE、受控读取；不激活消息 POST 完整 Tool 结果 | 否 |
| 1A | M7 ToolResult 与 Explanation | 完整结果链、selected 引用、稳定 Task 状态，并激活消息 POST 正式 Tool 响应 | 否，Mock LLM |
| 1A | M8 幂等、重试和失败 | 不重复写、Tool/Explanation 重试、安全错误映射 | 否 |
| 1A | M9 统一时间线 | 稳定锚点、游标、Task 卡聚合 | 否 |
| 1A | M10 最小 Vue 前端 | 浏览器聊天、结果卡、图片、重试 | 否 |
| 1A | M11 Mock 全链路验收 | 一键启动与完整 Mock E2E 报告 | 否 |
| 真实能力 | M12 真实 LangChain / LLM Provider | 真实三类编排、Explanation、安全失败；Mock 继续用于测试 | 否 |
| 1B | M13 Python 3.8 与权重加载 | 实际精确依赖、SHA-256、模型加载兼容记录 | 只加载，不推理 |
| 1B | M14 真实最小推理 | 单张 SEM、性能预测、分项耗时/资源/图像记录 | 是，首次推理 |
| 1B | M15 真实 Runtime 与 Adapter | 受 Token 保护的真实 `/internal/v1` | 是 |
| 1B | M16 真实 LLM + 真实 Runtime E2E | Vue→Backend→真实 LLM→真实 Runtime→MinIO→时间线 | 是 |

## 未来文件结构锁定

以下结构用于约束对应里程碑实现；是否在当前工作单元创建，以动态进度和当前里程碑允许范围为准：

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
│  └─ check-sem-integrity.ps1
└─ acceptance/
docs/
├─ superpowers/{specs,plans}/
├─ progress/phase-1-current-status.md
└─ acceptance/
   └─ sem-package-manifest.json
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
| 真实 LLM | 三类 structured output、Explanation、认证/限流/超时/非法输出 | `conda run -n materialsagent-backend python -m pytest backend/tests/contract/test_real_llm_adapters.py backend/tests/integration/llm -q` |
| 真实 Runtime 兼容 | 权重、图像、参数、耗时、显存 | `conda run -n materialsagent-zta35g python -m pytest zta35g-runtime/tests/compatibility -q -s` |
| 前端 | 类型、组件、最小交互 | `npm --prefix frontend run test -- --run` |
| Mock E2E | 公共请求到 Mock Runtime/MinIO/时间线 | `conda run -n materialsagent-backend python -m pytest backend/tests/e2e/test_mock_journey.py -q -s` |
| 真实 E2E | 公共请求到真实模型结果 | `conda run -n materialsagent-backend python -m pytest backend/tests/e2e/test_real_zta35g_journey.py -q -s` |

每个里程碑必须同时满足：列出的成功、输入错误、依赖失败、幂等/重试场景均有自动化证据；人工验收命令输出与预期一致；`git diff --check` 为零错误；`git status --short` 只出现本工作单元允许文件；`phase-1-current-status.md` 已更新；项目负责人确认暂停点后才能继续。

---

## G0：阶段 0 收尾与阶段 1 交接基线

**目标：** 建立仓库内长期规则和动态交接，保护未跟踪 `SEM/`，处理 `.codex/` 项目配置，并形成进入 M0 前可解释、可复核的 Git 状态。

**用户价值：** 新 Codex 对话可以从仓库恢复真实上下文，项目负责人可以明确审阅本地模型包是否变化、哪些配置可跟踪以及下一步是否仍处于暂停点。

**前置条件：** 五份设计基线和当前阶段 1 计划已提交；开始前重新记录 branch、HEAD、status、`git diff --check` 和最近提交。若与负责人描述不一致，停止实施性修改。

**允许修改的文件范围：** `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`、根 `AGENTS.md`、根 `.gitignore`、`docs/progress/phase-1-current-status.md`、`docs/acceptance/sem-package-manifest.json`、`scripts/dev/check-sem-integrity.ps1`；只有确认无 Secret、个人绝对路径和机器专用状态时，才可处理 `.codex/config.toml` 或创建脱敏的 `.codex/config.example.toml`。

**明确不做：** 不修改五份设计基线，不修改/移动/删除/运行 `SEM/`，不创建 Backend、Frontend、Mock Runtime 或真实 Runtime 业务代码，不安装依赖、创建环境/容器/数据库，不开始 M0，不暂存或 commit。

**实现步骤：**

1. 完整读取根 `.gitignore`、根 `AGENTS.md`、`.codex/` 全部实际文件、阶段 1 计划、五份基线、仓库结构和 `SEM/` 文件清单。
2. 编写简洁、长期稳定的根 `AGENTS.md`，只保存项目定位、权威索引、永久边界、工作方式、Git 规则和固定汇报格式。
3. 创建动态 `phase-1-current-status.md`，只保留当前状态、最近一次完成摘要和下一步；设计变更进入 specs，实施步骤变更进入本计划，动态事实进入进度文件。
4. 保留 `.gitignore` 原规则，只追加 `/SEM/` 和 `.codex/` 最小 allowlist；`.codex` cache/logs/sessions/tmp/worktrees 等状态始终忽略，只让明确安全的 `config.toml`/`config.example.toml` 保持可跟踪。
5. 只读枚举 `SEM/` 全部文件，稳定排序并记录相对路径、字节数和 SHA-256；聚合指纹使用本 manifest 声明的可重复规范算法。
6. 创建只读 `check-sem-integrity.ps1`，验证 manifest、文件集合、字节数、逐文件 SHA-256 和聚合指纹，不导入 Python 或运行模型。
7. 连续运行两次完整性检查，确认输出和退出码相同且不产生文件。
8. 更新进度文件为 `AWAITING_PROJECT_OWNER_REVIEW`，准确记录 branch、HEAD、验证、工作区、风险和下一步。
9. 复核五份基线、`SEM/`、业务代码范围、敏感信息、`.codex/` 待提交范围和 Git diff；到此停止。

**稳定退出码：** `0=SEM_INTEGRITY_OK`、`2=MANIFEST_NOT_FOUND`、`3=SEM_ROOT_NOT_FOUND`、`4=FILE_SET_MISMATCH`、`5=FILE_SIZE_MISMATCH`、`6=FILE_HASH_MISMATCH`、`7=MANIFEST_INVALID`。

运行：连续执行两次 `powershell -ExecutionPolicy Bypass -File scripts/dev/check-sem-integrity.ps1`，并执行 `git diff --check`、`git status --short`。

预期：两次均退出 0 且输出完全相同；文件数、总字节数和 aggregate fingerprint 与 manifest 一致；`SEM/` 不进入普通 Git 状态；只有 G0 允许文件和经审阅的安全 `.codex` 配置可见。

**人工验收步骤：** 项目负责人审阅计划修订、`AGENTS.md`、动态进度、`.gitignore` 追加规则、`.codex/` 决定、manifest 重点文件和两次完整性检查证据。

**失败时回退：** 只回退 G0 本轮文件；不修改五份基线、`.codex` 原始本地配置或 `SEM/`，不删除负责人已有未提交内容。

**完成与提交门：** G0 本轮状态保持“待项目负责人复核”。项目负责人验收后才允许显式暂存批准文件并提交；G0 提交完成且工作区干净后，才允许开始 M0。本轮不执行 `git add` 或 `git commit`。

## M0：仓库准备

**目标：** 在 G0 交接基线上完善未来目录、文本规范、开发命令和工作单元范围检查，确保后续实现可独立审计。

**用户价值：** 项目负责人可以在任何实现开始前看清“会创建什么、不会碰什么”，避免研究模型目录或已确认文档被误改。

**前置条件：** 五份文档均为已确认设计基线；项目负责人已验收并批准提交 G0；G0 提交完成且工作区干净；负责人明确允许开始 M0。

**允许修改的文件范围：** `.editorconfig`、`.gitattributes`、`.env.example`、根 `README.md`、`environments/README.md`、`scripts/dev/check-scope.ps1`、`docs/acceptance/phase-1-checklist.md`。不创建业务包。

**明确不做：** 不重新创建或覆盖根 `AGENTS.md`、初始 current status、初始 SEM manifest 或完整性脚本；不创建 Conda 环境、Compose、Backend/前端/Runtime 代码，不改五份基线和 `SEM/`。

**实现步骤：**

1. 写入目录与命名规范、UTF-8/LF 规则和 Windows PowerShell 命令约定。
2. `.env.example` 只列变量名与安全说明：数据库、MinIO、LLM、Mock/真实 Runtime URL、共享 Token；不写真实 secret。
3. `check-scope.ps1` 读取 `git status --short`，在当前工作单元允许范围之外出现文件时以非零退出；它必须调用或配合 `scripts/dev/check-sem-integrity.ps1`，使已被 `.gitignore` 忽略的 `SEM/` 仍受完整性检查。
4. `phase-1-checklist.md` 列出 G0、M2、M4、M7、M8、M11、M12、M13、M14、M16 十个暂停点和每个阶段禁止项。

**自动化测试与四类场景：**

- 成功：允许范围内文件被脚本接受，且 SEM 完整性检查为 `SEM_INTEGRITY_OK`。
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
2. 创建 Python 3.11 环境文件和 Backend 包；`backend/pyproject.toml` 是 Python 包依赖和版本约束唯一事实来源，Conda YAML 只声明环境名、Python、pip 和必要系统级依赖；环境创建后形成已安装版本记录。
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
2. Compose 只定义 `postgresql`、`minio` 与初始化 bucket 的受控配置；两种镜像都使用明确版本标签，禁止 `latest`；volume 名固定但不提交数据。
3. 建立 Alembic 基线和 Actor migration，Repository/UoW 用短事务。
4. 实现 MinIO Adapter 与安全错误分类；ready 的两个探针各自有短超时。

**自动化测试与四类场景：** 成功创建/读取本地匿名 Actor并 put/get 测试对象；输入错误拒绝非法 object key；依赖失败分别停止 PostgreSQL 或 MinIO 并得到 NOT_READY/DEGRADED；同 actor_id/同对象写重放不产生重复行或错误覆盖。

运行：`docker compose up -d postgresql minio`；`conda run -n materialsagent-backend alembic -c backend/alembic.ini upgrade head`；`conda run -n materialsagent-backend python -m pytest backend/tests/integration/db backend/tests/integration/storage backend/tests/api/test_health.py -q`

预期：Compose 只有两个业务依赖服务；migration 到唯一 head；0 failed；ready 不返回连接串、端口、bucket 或 secret。

**人工验收步骤：** `docker compose ps` 显示 PostgreSQL/MinIO healthy；停止 MinIO 后 ready 为 DEGRADED 或 NOT_READY 的基线映射，live 保持 200；恢复后 ready 恢复。

**失败时回退：** `docker compose down` 停服务；Alembic 只在本地空库按 downgrade 验证，禁止对已含验收数据的库破坏性回退；M1 仍可独立运行。

**完成证据：** Compose 服务清单、Alembic heads、集成测试、故障注入响应、`git diff --check`。到此必须暂停，等待项目负责人通过 M2 检查点。未来建议提交：`feat: add postgres and minio foundations`。

## M3：Conversation、Message、Task 最小闭环

**目标：** 纵向实现 Actor 所有权下的 Conversation 创建、UserMessage/Task 原子创建和 Task 查询，不接 LLM/Tool。

**用户价值：** 用户请求第一次成为可靠、可查询、不会留下半条消息的业务事实。

**前置条件：** M2 项目负责人检查点通过。

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

**目标：** 实现可替换 Chat Orchestration Port、Mock LLM、确定性单位/参数校验、正式持久化和公共 HTTP 映射，覆盖知识回答、NEEDS_INPUT、硬校验失败、编排依赖失败，以及完整合法 ToolCandidate 在完整 Tool 结果链尚未开放时的安全不可用终态。

**用户价值：** 用户能收到知识回答或准确追问，完整非法输入不会误执行 Tool；完整合法输入在当前能力尚未开放时得到明确、安全、可追溯的 `TOOL_UNAVAILABLE`，不会被伪装成 Tool 已成功执行。

**前置条件：** M3 通过；M4-A 的候选契约、Mock Adapter、确定性标准化/硬校验和 LLMCall 基础已验收提交。

**允许修改的文件范围：** `domain/ports/chat_orchestration.py`、`application/{chat_orchestration,normalization,validation}.py`、`infrastructure/llm/mock.py`、LLMCall migration/repository、messages API 和 M4 tests。

**明确不做：** 不接真实 LLM，不创建 Tool Registry、MaterialTool、ToolRun、Runtime、Asset、ToolResult 或 Explanation，不运行 Mock Runtime，不让 LLM 负责最终单位转换或范围判断。

**接口：** `ChatOrchestrationPort.orchestrate(input) -> KnowledgeAnswer | ToolCandidate | NeedsInputCandidate`；Application 将候选重新标准化和硬校验后转为正式 Message/Revision/Task/LLMCall 事实。完整合法 ToolCandidate 在 M7 激活完整结果链前固定映射为：

```text
LLMCall.status = SUCCEEDED
创建 TaskInputRevision revision 1
Task.task_type = TOOL_EXECUTION
Task.current_status = FAILED
Task.error_code = TOOL_UNAVAILABLE
Task.safe_error_message = 固定安全文本
HTTP = 503
不创建 ToolRun / ToolResult / Asset / 成功 AssistantMessage
selected_tool_run_id = null
selected_result_id = null
```

该路径表示 CHAT_ORCHESTRATION、Application 标准化和硬校验均成功，但平台尚未开放完整 Tool 执行与结果持久化能力；它不是输入校验失败，也不是 Runtime 执行后失败。

**实现步骤：**

1. M4-A 先为三类判别联合、温度/时间单位、精度、范围、unsupported material 和 Mock 安全错误写失败测试，并实现受控候选、Mock Adapter、确定性规则和 LLMCall 基础。
2. M4-B 为知识回答、NEEDS_INPUT、歧义、硬校验非法、timeout/provider/protocol、数据库终结失败和重复终结写失败测试。
3. M4-B 使用短事务创建/启动/终结 LLMCall，Mock 调用只发生在 UnitOfWork/Session 之外；Application 必须重新计算正式 missing/ambiguous/validation 字段。
4. 在消息 POST 中同步完成 KNOWLEDGE_ANSWER=200、NEEDS_INPUT=200、完整但硬校验非法=422，以及安全的 CHAT_ORCHESTRATION 失败映射。
5. 对完整合法 ToolCandidate 创建正式 revision 1，将 LLMCall 终结为 SUCCEEDED、Task 终结为 TOOL_EXECUTION/FAILED，并返回 503 `TOOL_UNAVAILABLE`；不得创建 ToolRun、占位结果或模板 AssistantMessage。

**自动化测试与五类业务场景：** 知识回答→AssistantMessage + Task SUCCEEDED；缺失/歧义→正式 Revision + NEEDS_INPUT；完整但硬校验非法→Revision + 422 FAILED；timeout/provider/protocol→LLMCall/Task 安全 FAILED 且无伪造事实；完整合法 ToolCandidate→Revision + LLMCall SUCCEEDED + Task TOOL_EXECUTION/FAILED + 503 `TOOL_UNAVAILABLE`，无 ToolRun。相同内部调用结果重复终结不得创建第二 AssistantMessage 或第二个 revision 1。

运行：`conda run -n materialsagent-backend python -m pytest backend/tests/unit/test_zta35g_input.py backend/tests/unit/test_chat_orchestration_service.py backend/tests/contract/test_chat_orchestration.py backend/tests/integration/db/test_chat_orchestration_persistence.py backend/tests/api/test_message_orchestration.py backend/tests/api/test_tasks.py -q`

预期：三类 route 和五类业务映射均有测试；0 failed；完整合法 ToolCandidate 为 503/`TOOL_UNAVAILABLE` 且数据库无 ToolRun；日志可见 request_id/task_id/LLMCall，但无 Prompt、provider 原始响应、路径或 Secret。

**人工验收步骤：** 分别提交知识问题、缺 aging_temperature、solution_time=180 min、越界温度、完整合法 Tool 请求和 Mock timeout；核对 HTTP、Task、Revision、AssistantMessage、LLMCall 和数据库行。完整合法 Tool 请求必须返回 503/`TOOL_UNAVAILABLE`、LLMCall SUCCEEDED、Task FAILED，且 ToolRun/Asset/ToolResult 行数均为 0。

**失败时回退：** 配置切回无 Orchestration 的 M3 路径，仅用于开发诊断；不得在产品验收中伪造成功。

**完成证据：** 五类业务响应、单位转换快照、超时/provider/protocol 响应、LLMCall 安全投影、完整合法候选的 503 与零 ToolRun 证据、diff 检查。M4 完成后必须暂停，等待项目负责人验收；不得直接开始 M5。未来建议提交：`feat: add mock chat orchestration`。

## M5：Mock Runtime、Tool Registry 与 ToolRun

**目标：** 建立静态 Tool Registry/Catalog、独立 Mock Runtime、HTTP Client Adapter、MaterialTool、ToolRun 和 ToolExecutionService 的内部执行闭环；不把尚无 Asset/ToolResult 的 Mock 成功投影为公共 Task 完整成功。

**用户价值：** 在无真实模型情况下验证后端内部确实会调用一个符合第四节 B 的 Tool，并能追踪 ToolRun、区分未就绪、繁忙、超时和协议错误；消息 POST 仍不会向用户伪造完整 Tool 结果。

**前置条件：** M4 通过项目负责人验收并提交；完整合法消息请求的 M4 终态仍为 503/`TOOL_UNAVAILABLE`。

**允许修改的文件范围：** `mock-runtime/**`、Backend Tool Registry/Catalog、MaterialTool/ToolExecutionOutput ports、ToolRun model/repository/migration、tool_clients、tools routes、M5 tests。

**明确不做：** 不保存 Asset/ToolResult/Explanation，不加载 `SEM/`，不自动重试 execute，不建立 Runtime 队列或让 Backend 启动 Runtime；不解除消息 POST 的 M4 `TOOL_UNAVAILABLE` 映射，不因 Mock Runtime 返回成功而把公共 Task 标为 SUCCEEDED/PARTIALLY_SUCCEEDED。

**接口：** Mock 提供 token 保护的 live/ready/execute；Adapter 只发送三个 ID、固定 Tool/版本、四维参数、requested_outputs、seed 与 `1/2.0/1000`。

**实现步骤：**

1. 先写 Token、固定参数、版本、busy=1、超时、无自动重试和响应 ID 回显契约测试。
2. Mock Runtime 返回确定性 Base64 `.npy` 和可注入的安全失败，不访问数据库/MinIO。
3. 实现 Registry 唯一事实源和 Catalog 投影；MaterialTool 只通过 Adapter 调用 Runtime。
4. ToolRun 创建、启动和调用失败终结使用短事务；HTTP 调用在事务外。成功 Mock 调用只形成受控的待提交 ToolExecutionOutput/诊断事实，ToolRun/Task 的完整成功聚合等待 M7 的 Asset/ToolResult 链。

**自动化测试与四类场景：** 成功创建一个 ToolRun 并得到一次受控 ToolExecutionOutput；输入错误 guide_scale=3.0 或未知 Tool 被 Runtime 拒绝；依赖失败 Runtime 未启动/超时/忙正确映射；同一 ToolRun 不自动重发，显式第二次执行必须新 ID/seed（公共端点留 M8）。另加回归断言：消息 POST 的完整合法 ToolCandidate 仍返回 503/`TOOL_UNAVAILABLE`，无 Asset/ToolResult/selected 引用，不把 Mock Runtime 成功冒充为用户结果。

运行：`conda run -n materialsagent-backend python -m pytest mock-runtime/tests backend/tests/contract/test_runtime_contract.py backend/tests/integration/db/test_tool_run.py backend/tests/api/test_tools.py backend/tests/api/test_message_orchestration.py -q`

预期：0 failed；Mock ready 不推理；第二并发 execute 为 503 `RUNTIME_BUSY`、`retryable=true`；Adapter 调用计数在超时后仍为 1；公共消息 POST 未被激活为完整 Tool 成功。

**人工验收步骤：** 手动启动 Mock Runtime，先带 Token 检查 ready，再通过受控开发验收路径用已校验 Revision 调用 ToolExecutionService 并查询 ToolRun；用错误 Token 验证三条内部路径均拒绝。另提交完整合法消息请求，确认仍为 503/`TOOL_UNAVAILABLE`，没有结果、图片或 selected 引用。

**失败时回退：** Backend 配置回到 Tool unavailable；M4 知识回答/NEEDS_INPUT 仍可运行。删除 Mock 不影响平台数据库。

**完成证据：** Mock 请求/响应样例、固定参数断言、busy 并发测试、ToolRun 与待提交输出记录、消息 POST 未误报成功的证据、日志三 ID、diff 检查。未来建议提交：`feat: add mock zta35g runtime slice`。

## M6：Asset PENDING → AVAILABLE

**目标：** 解码并校验 Mock `.npy`，由 Backend 正式编码 PNG，完成 Asset PENDING→MinIO→AVAILABLE/FAILED/ORPHANED 与受控内容读取；不在 ToolResult 尚未建立时激活消息 POST 的完整 Tool 结果。

**用户价值：** 用户可以通过独立 Asset 验收路径可靠查看生成图片，且图片缺失或损坏时不会被误报为成功；该能力本身不代表原消息 Task 已形成完整 Tool 结果。

**前置条件：** M5 通过。

**允许修改的文件范围：** Asset model/repository/migration、`application/asset_service.py`、图片校验/PNG 编码、StorageService Adapter、assets API、M6 tests。

**明确不做：** Runtime 不编码 PNG、不上传 MinIO；不实现真实上传、签名 URL、后台 orphan 清理器；不创建 ToolResult/Explanation，不解除消息 POST 的 `TOOL_UNAVAILABLE` 映射。

**接口：** ImagePayload 严格 `<f4`、`[512,512]`、二维 C-order、有限值、`[-1,1]`、`base64+npy`、`allow_pickle=False`；AssetService 执行 PENDING→put→AVAILABLE。

**实现步骤：**

1. 写 NaN/Inf、object dtype、shape、dtype、Base64、4 MiB、hash、PNG 量化和 MinIO 故障测试。
2. 实现内存 `.npy` 安全解码与第二节固定 PNG 映射。
3. 实现两个短事务和 MinIO put/head；失败落 FAILED/ORPHANED 锚点。
4. 实现 Asset 元数据/content API、Actor 所有权、inline/attachment。
5. 保持消息 POST 完整合法 ToolCandidate 为 503/`TOOL_UNAVAILABLE`；AVAILABLE Asset 不得单独使 Task 成为 SUCCEEDED/PARTIALLY_SUCCEEDED，也不设置 selected 引用。

**自动化测试与四类场景：** 成功得到 AVAILABLE mode-L PNG；输入错误非法 `.npy`→INVALID_MODEL_OUTPUT；依赖失败 MinIO put/AVAILABLE Tx2 故障→FAILED/PENDING 可恢复；同一 asset 终结重试不重复创建对象且状态条件更新安全。回归验证消息 POST 仍不返回完整 Tool 成功，AVAILABLE Asset 不被冒充为 ToolResult。

运行：`conda run -n materialsagent-backend python -m pytest backend/tests/unit/test_image_payload.py backend/tests/unit/test_png_encoder.py backend/tests/integration/storage/test_asset_lifecycle.py backend/tests/api/test_assets.py backend/tests/api/test_message_orchestration.py -q`

预期：0 failed；PNG 512×512、8-bit、无 alpha；非 AVAILABLE content=409；响应无 object_key。

**人工验收步骤：** 使用 M5 受控内部执行输出建立 Asset，下载 inline PNG 并用图片查看器打开；显式 attachment 检查响应头；停止 MinIO 后确认不返回成功图片。另提交完整合法消息请求，确认仍为 503/`TOOL_UNAVAILABLE`，不返回 result_summary/explanation/selected 引用。

**失败时回退：** 保留 ToolRun 诊断，把相关 Asset 标为 FAILED/ORPHANED；禁用 Asset 内容端点，不修改 M5 ToolRun 历史。

**完成证据：** PNG 元数据/SHA-256、MinIO HEAD、失败恢复记录、API 头、消息 POST 未误报结果完成的证据、diff 检查。未来建议提交：`feat: add generated asset lifecycle`。

## M7：ToolResult 与 Explanation

**目标：** 在 Asset AVAILABLE 后提交 ToolResult/ResultAssetLink，用 Mock Explanation Port 生成独立解释，更新 selected references 与 Task 稳定最终状态，并从本里程碑开始激活消息 POST 的正式 Tool 成功/部分成功/失败响应。

**用户价值：** 用户第一次能从原消息 POST 获得完整、已持久化的结构化性能、图片和解释；解释失败时已有结果不会丢失。

**前置条件：** M6 通过。

**允许修改的文件范围：** ToolResult、ResultAssetLink、Explanation、LLMCall models/repositories/migrations，result/explanation services、ToolExecutionService 结果提交接线、ChatOrchestrationService/消息 API 激活接线、tool-results routes 和 M7 tests。

**明确不做：** 不接真实 LLM，不复制 ToolResult/Explanation 为 Message，不跨 ToolRun 混合 Asset，不实现重试端点（M8）。

**接口：** ResultService 在一个短事务校验 ToolRun/Asset/Task 来源并提交 Result、links、selected 引用；Explanation 只读已提交 Result。只有完整链路能够形成已持久化稳定状态时，消息 POST 才从 M4 的 503/`TOOL_UNAVAILABLE` 暂态映射切换为正式 Tool 执行。

**实现步骤：**

1. 写跨 Task/ToolRun Asset、非 AVAILABLE、部分成功、仅性能失败、解释失败/超时测试。
2. 实现 ToolResult 输出集合与状态聚合，示例性能值只来自 Mock 响应。
3. 实现 Explanation prepare→事务外 Mock LLM→finalize；不保存 Prompt。
4. 把 M5 的内部 Tool 执行与 M6 的 Asset 生命周期接入 ResultService，完成 ToolRun→Asset→ToolResult/ResultAssetLink→Explanation→selected references→Task 稳定状态。
5. 激活消息 POST：完整合法 ToolCandidate 进入上述正式链路，并只从已持久化事实组装成功、部分成功或失败响应；不使用 202。若结果链未启用或无法启动，继续使用安全 `TOOL_UNAVAILABLE`，不得返回占位成功。

**自动化测试与四类场景：** 消息 POST 完整成功形成 Result+Asset+Explanation 和 selected 引用；输入错误跨来源 Link 被拒；依赖失败 Explanation 超时→HTTP 200/Task PARTIALLY_SUCCEEDED 且 Result 保留；重复 Explanation finalize 不覆盖旧事实。另覆盖图片成功/性能失败、只请求性能失败、Asset/Result 持久化失败，以及结果链未开放时不得绕过 `TOOL_UNAVAILABLE`。

运行：`conda run -n materialsagent-backend python -m pytest backend/tests/integration/db/test_result_commit.py backend/tests/contract/test_tool_execution_output.py backend/tests/api/test_message_orchestration.py backend/tests/api/test_tool_results.py backend/tests/api/test_explanation_outcomes.py -q`

预期：0 failed；完整合法消息 POST 不再固定返回 `TOOL_UNAVAILABLE`，而是进入正式执行并返回已持久化稳定事实；ToolResult 成功后 Explanation 失败仍返回结构化结果；数据库无跨 ToolRun link。

**人工验收步骤：** 从消息 POST 演示全部成功、图片成功/性能失败、只请求性能失败、解释超时四种响应；逐项核对 ToolRun、Asset、ToolResult/ResultAssetLink、Explanation、selected 引用和 Task 状态均来自同一来源链。

**失败时回退：** Explanation Adapter 可切换为明确失败替身；已提交 ToolResult/Asset 不回滚，不重跑 Tool。若 M7 完整链无法安全激活，消息 POST 回到 M4 的 503/`TOOL_UNAVAILABLE`，保留 M5/M6 内部事实但不向用户冒充完整结果。

**完成证据：** 消息 POST 正式 Tool 响应、四类结果、来源约束测试、selected 引用、Explanation LLMCall、M4 暂态映射只在完整结果链可用时解除的证据、diff 检查。M7 完成后必须暂停，等待项目负责人验收消息 POST 激活点；不得直接开始 M8。未来建议提交：`feat: add tool result and explanation slice`。

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

**完成证据：** 并发计数、资源行数、重放响应、超时/busy 映射、安全扫描、diff 检查。到此必须暂停，等待项目负责人通过 M8 检查点。未来建议提交：`feat: add idempotency and explicit retries`。

## M9：Conversation 统一时间线 API

**状态与执行拆分：** 项目负责人已批准在同一 Codex 对话内依次完成 `M9-P → M9-A → 内部 gate → M9-B → 全量验证`；M9-P 只固化本节方案、scope allowlist 和动态状态，M9-A 完成查询基础，内部 gate 通过后才允许接入公共路由与扩展 Task GET。

**目标与用户价值：** 由 Backend 组装 `USER_MESSAGE`、`ASSISTANT_MESSAGE`、`TOOL_TASK` 判别联合。普通 `KNOWLEDGE_QA` 和 `task_type=NULL` Task 的消息作为顶层 Message；`TOOL_EXECUTION` Task 只形成一个 Tool Task 卡，其初始 UserMessage 不重复成为顶层项。用户能在一条历史中看见知识问答和 Tool 卡，重试不会把旧卡移动或重复展示结果。

**前置条件：** M8 项目负责人检查点已通过，实时基线为 `main@18005944982ca5191412e06154effc67465ca3a7`（`feat: add idempotent task retries`），工作区和暂存区为空。

**允许修改的文件范围（精确 allowlist）：**

- 计划、状态、scope 与安全配置示例：本计划、`docs/progress/phase-1-current-status.md`、`scripts/dev/check-scope.ps1`、`.env.example`。
- 迁移：`backend/alembic/versions/0009_add_timeline_query_indexes.py`。
- 生产代码：`backend/src/materialsagent/application/timeline.py`、`backend/src/materialsagent/application/timeline_cursor.py`、`backend/src/materialsagent/application/tasks.py`、`backend/src/materialsagent/domain/ports/timeline_query.py`、`backend/src/materialsagent/infrastructure/config.py`、`backend/src/materialsagent/infrastructure/db/conversation_task.py`、`backend/src/materialsagent/infrastructure/db/timeline_query.py`、`backend/src/materialsagent/api/dependencies.py`、`backend/src/materialsagent/api/routes/timeline.py`、`backend/src/materialsagent/api/routes/tasks.py`、`backend/src/materialsagent/main.py`。
- 测试：`backend/tests/unit/test_config.py`、`backend/tests/unit/test_timeline_cursor.py`、`backend/tests/unit/test_timeline_sort.py`、`backend/tests/unit/test_task_query.py`、`backend/tests/contract/test_timeline_contract.py`、`backend/tests/integration/db/test_migrations.py`、`backend/tests/integration/db/test_timeline_query.py`、`backend/tests/api/conftest.py`、`backend/tests/api/test_timeline.py`、`backend/tests/api/test_tasks.py`。
- 仅在建立必要共享夹具时允许：`backend/tests/conftest.py`、`backend/tests/integration/db/conftest.py`。

**明确不做：** 不建 TimelineItem 表，不复制 Result/Explanation 到 Message，不实现 SSE/SSE 游标，不让前端自行排序；不修改五份设计基线、`0001`–`0008`、Tool/重试/写路径、`SEM/`、Mock Runtime 或前端；不引入 Redis、Worker、登录、多 Tool、真实模型或新的外部调用；不执行 `git add`、commit、push、amend，也不开始 M10。

### M9-P：实施方案、scope 和动态状态

1. 将本节固化为完整 M9 方案；为 M9 增加精确 allowlist，保持 M0–M8 allowlist 不变；按实时 Git 更新动态状态。
2. 运行 `check-scope.ps1 -Milestone M9`、`git diff --check`，确认 M9-A 尚未开始后进入测试先行。

### M9-A：配置、签名游标、排序与专用查询基础

1. 先写失败测试，再实现 `TIMELINE_CURSOR_SIGNING_KEY`：配置字段为可选 `SecretStr`，缺失不影响其他 API；Timeline service 缺失时由依赖返回安全 503。有效 key 必须原样非空、无首尾空白/控制字符，UTF-8 至少 32 bytes；不提供默认或回退 secret，`.env.example` 只给安全占位说明。
2. 游标固定为 canonical JSON（`sort_keys=True`、紧凑分隔、`allow_nan=False`）→ UTF-8 → HMAC-SHA256 → base64url，不透明 token；payload 精确包含 `version=1`、`conversation_id`、UTC RFC3339 `anchor_at`、`item_type_rank`、`item_id`。使用 `compare_digest`，最大 token 2048 字符、解码 JSON 最大 512 bytes；任何格式、签名、版本、跨 Conversation、非 UTC 或多余/缺失字段问题统一抛 `InvalidCursorError`，公共 422 文案为“分页游标无效。”。
3. 稳定顶层 key 为 `(anchor_at, item_type_rank, item_id)`，rank 固定 USER=10、ASSISTANT=20、TOOL_TASK=30。Tool Task `anchor_at` 取该 Task 最早 USER Message 的 `created_at`，不存在才回退 `Task.created_at`；重试只改变卡片聚合内容，不移动 anchor。
4. 审计实际索引；若缺失，新增唯一 head `0009_timeline_query_indexes`（`down_revision=0008_idempotency_record`），只为 Message `(conversation_id, created_at, message_id)` 与 Task `(conversation_id, created_at, task_id)` 建索引，并同步 ORM metadata；覆盖 `0008 → 0009 → 0008 → 0009` 往返与 Alembic check。
5. 新建独立 domain Timeline Query Port 和 SQLAlchemy adapter。每次读取使用一个短 `REPEATABLE READ READ ONLY` 事务，不进入普通写 UoW、不加锁、不提交写入、不调用 Runtime/MinIO/LLM。
6. 使用两阶段批量查询：第一阶段以 UNION/CTE 在数据库完成 keyset 排序、`limit+1` 和 cursor 边界；第二阶段按本页 ID 批量读取 Task、Message、Revision、全部 ToolRun、selected Result、ResultAssetLink/Asset、Explanation/LLMCall。`limit=1` 与 `limit=20` 的 SELECT 数相同或固定常数，目标不超过 10，禁止按卡片 N+1 和全量 Python 排序/切片。
7. 应用服务组装安全投影：input thread 为初始消息加最新最多 50 条其余消息并恢复升序，同时返回 count/truncated；卡片只展开 selected run/selected result，保留 `attempt_count`/`has_history`；ToolRun diagnostics 仅允许固定安全字段；Result 只包含受控字段；Asset 仅返回同 selected Result link、同 selected run、`AVAILABLE` 的安全 URL，按 `(artifact_order, asset_id)` 排序；Explanation 复用 `select_explanation_attempts()`，只输出受控成功正文或安全失败摘要。
8. 对 Conversation/Task/Message Actor 与 Conversation、selected Run/Result/output sets、ResultAssetLink/Asset/Run/Task、Explanation/LLMCall/Result/Task 来源做严格一致性校验。所有损坏视为内部数据完整性错误，安全 500；不得误报 422、不得泄漏内部路径、对象 key、model bundle、provider 原始信息或 traceback。
9. M9-A gate 必须确认游标、排序、migration、数据库投影、只读事务、固定查询数、完整性矩阵和 scope 全绿；同时明确公共 timeline route 和 Task GET 扩展仍未开始。

### M9-B：公共 Timeline API 与 Task GET 扩展

1. 先写 Timeline API 与 contract 失败测试。新增 `GET /api/v1/conversations/{conversation_id}/timeline`，`limit` 默认 20、范围 1–50，游标为上述签名 token；响应模型全部 `extra="forbid"`，使用 `item_type` 判别 USER_MESSAGE、ASSISTANT_MESSAGE、TOOL_TASK union；顶层 Message 与 Tool 初始 UserMessage 互斥。
2. route 只调用 Timeline application service；依赖注入在有数据库 engine 和有效 signing key 时构造 service，缺少 key 仅让 Timeline 返回 503，不影响 health/Conversation/Task 等既有接口。非法/篡改/跨 Conversation cursor 为安全 422；其他 Actor 与不存在 Conversation 均为一致安全 404；数据库不可用为 503，数据损坏为 500。
3. 扩展 `GET /api/v1/tasks/{task_id}`，保持既有字段并增加 `anchor_at`、`needs_input`、`tool_run_count`、按 `(created_at, attempt_no, tool_run_id)` 排序的全部 `tool_runs`、selected Result 安全摘要、Explanation 选择与最近失败摘要。整个 Task projection 通过专用 TaskDetailQueryPort，在单个短 REPEATABLE READ READ ONLY 查询事务中读取并组装 TaskDetailQuerySnapshot；不进入普通写 UoW，不加锁、不写数据库，也不调用任何外部 adapter。完整性损坏为安全 500，所有权隔离仍为 404。
4. 回归 OpenAPI/contract，确保三个判别 item、严格 schema、UTC `Z` 时间、安全字段和旧 API 兼容性。

### 验证、人工验收与停止点

1. 聚焦运行 cursor/config/sort/task unit、Timeline DB、Timeline API/contract/Task API；再运行全部 contract、全部 Backend、Mock Runtime、`pip check`、`compileall`。
2. 运行 Alembic heads/upgrade/current/check、`0009 → 0008 → 0009` 往返并确认两个索引；运行 M9 scope、SEM integrity 与完整 Git 审计。
3. 使用受控 fixture/一次性数据库走真实 HTTP：混合知识问答/Tool 时间线、同时间戳顺序、分页、旧 cursor 在 Tool/Explanation retry 后 anchor 不变、Task 全历史、所有权/篡改/缺 key/数据损坏安全映射；不得为验收触发昂贵真实 Tool。
4. 最终更新动态状态为 `M9 COMPLETE / PROJECT_OWNER_REVIEW AWAITING`，保留未暂存改动，停止在项目负责人验收点；不执行提交或 M10。

**失败时回退：** 保留 Task/Result 详情 API，暂时关闭 timeline 路由；不改变底层事实。迁移失败时回到 `0008_idempotency_record` 并停止，不修改历史 migration。

**完成证据：** 排序与分页夹具、批量查询计数与只读隔离级别、完整性损坏矩阵、API/OpenAPI schema、迁移往返、人工 HTTP 快照、全量测试、scope/SEM/Git diff。未来建议提交（仅供负责人后续批准）：`feat: add stable conversation timeline`。

## M10：最小 Vue 3 + Vite 前端

**状态与执行拆分：** M10 明确拆成两个独立负责人验收工作单元：

1. `M10-A：前端基础与可靠数据层`；
2. `M10-B：完整最小界面与交互`。

M10-A 和 M10-B 分别停在项目负责人验收点。M10-A 获批前不得开始 M10-B；M10-B 获批前不得开始 M11。

**总目标与用户价值：** M10-A 先建立可测试、可构建且能可靠消费当前公共 API 的 Vue 数据层；M10-B 再在该数据层上建立 Conversation 列表、聊天输入、三类 TimelineItem、Tool Task 状态卡、Result/Asset/Explanation、补参和两类重试。项目负责人最终无需直接阅读 JSON 即可完成主要产品验收。

**前置条件：** M9 已由项目负责人验收并提交；M10-A 的实时恢复基线为 `main@d7dee06c1f1294b010f64f5c532f5cb276ca53fa`（`feat: add stable conversation timeline`），工作区和暂存区为空，Node.js 为 `24.14.0`，npm 为 `11.9.0`。

### M10 已确认技术决定

1. 使用 Vue 3、Vite、TypeScript、Vitest、Vue Test Utils、jsdom、Composition API、普通 CSS、npm 和 `package-lock.json`。
2. 不使用 Pinia、Vue Router、Axios、Zod、MSW、Tailwind CSS、UI 组件库、Redux 风格状态库、复杂实时库、OpenAPI 类型生成或 Playwright。
3. 公共 API 类型使用手写严格 TypeScript 类型；动态 Tool 数据使用 `Record<string, unknown>`，不使用 `any`。
4. API Client 使用原生 `fetch`，开发环境通过 Vite `/api` 代理访问 Backend；M10 默认不修改 Backend CORS。
5. 写操作在 fetch 前持久化幂等描述符；网络或协议结果不确定时以 `sessionStorage` 保存原 operation、resource、body 和 key，不自动重放，只允许用户用原 key 重试或显式放弃。
6. 全局只允许一个 `SENDING` 或 `UNCERTAIN` 待定写操作。
7. 页面只使用一个轮询协调器；递归 `setTimeout` 保证同一时刻最多一个 GET poll，页面隐藏时暂停，重新可见时立即刷新。
8. Timeline 按 Backend 返回数组顺序逐页追加，不调用 `sort`，不根据 `updated_at`、完成时间或 Task 状态重排。
9. Timeline 使用 cursor 顺序读取全部页面；任何一页失败都不替换现有完整 Timeline，重复 cursor 或缺少必需 next cursor 视为安全协议错误。
10. Task GET 只在用户展开历史或明确需要活动 Task 详情时读取，不在每轮 Timeline poll 中为所有卡片查询。
11. Conversation/Timeline GET 同时使用 `AbortController` 和 generation token，防止旧 Conversation 的迟到响应覆盖新选择。
12. Asset 图片只使用公共 `content_url`；inline 展示，下载时安全追加 `disposition=attachment`，不构造 MinIO URL。

### M10-A：前端基础与可靠数据层

**M10-A 精确 allowlist：**

- 实施管理：本计划、`docs/progress/phase-1-current-status.md`、`scripts/dev/check-scope.ps1`、根 `.env.example`（仅当它继续承担仓库级环境变量索引时）。
- 配置与骨架：`frontend/.env.example`、`frontend/.gitignore`、`frontend/.node-version`、`frontend/index.html`、`frontend/package.json`、`frontend/package-lock.json`、`frontend/tsconfig.json`、`frontend/tsconfig.app.json`、`frontend/tsconfig.node.json`、`frontend/vite.config.ts`、`frontend/src/env.d.ts`、`frontend/src/main.ts`、`frontend/src/App.vue`、`frontend/src/styles.css`。
- API 与状态层：`frontend/src/api/types.ts`、`frontend/src/api/errors.ts`、`frontend/src/api/client.ts`、`frontend/src/composables/useIdempotentRequest.ts`、`frontend/src/composables/usePolling.ts`、`frontend/src/composables/useMaterialsAgent.ts`。
- 测试：`frontend/src/test/setup.ts`、`frontend/tests/api/client.test.ts`、`frontend/tests/composables/idempotent-request.test.ts`、`frontend/tests/composables/polling.test.ts`、`frontend/tests/composables/materials-agent.test.ts`。

**M10-A 明确不做：** 不创建 `frontend/src/components/**`、`frontend/src/views/**`、`frontend/src/stores/**`、`frontend/src/router/**`、`frontend/public/**`、`frontend/src/assets/**` 或 `frontend/README.md`；不实现完整产品界面、浏览器 E2E、登录、上传、SSE、WebSocket；不修改 Backend、CORS、五份设计基线、`SEM/`、Mock Runtime 或 M11 文件。

**M10-A 实现步骤：**

1. 先细化本节、为 M10-A 配置精确 scope、修正 M9 验收提交并把动态状态置为 `M10_A_IN_PROGRESS`；通过 scope、SEM 和 Git 内部 gate 后才创建前端。
2. 查询 npm 官方 registry 的稳定版本、engines 与 peer dependencies，选择支持 Node.js 24.14.0 的兼容组合；所有直接依赖使用精确版本，固定 `packageManager=npm@11.9.0`、Node `24.14.x`、npm `11.9.x`，生成并用 `npm ci` 验证 lockfile。
3. 创建 Vue/Vite/TypeScript/Vitest 最小骨架和安全环境配置；Vite 使用 `/api` 代理且只接受 `http/https` Backend origin，production bundle 不嵌入代理 origin。
4. 先写并运行 API Client 红测，再实现手写公共类型、有界安全错误、原生 fetch Client、公共 Asset URL 和 attachment helper。
5. 先写并运行幂等状态红测，再实现 `sessionStorage` descriptor、`SENDING/SUCCEEDED/UNCERTAIN/BUSINESS_FAILED` 状态、原 key 重试和显式放弃。
6. 先写并运行轮询红测，再实现递归 timer、可见性恢复、单并发、stop/abort 和 triggerNow 协调。
7. 先写并运行 `useMaterialsAgent` 红测，再实现 Conversation 分页、Timeline 全分页、服务端顺序、generation token、显式 Task history、补参目标和四类幂等写操作。
8. 全量运行 `npm ci`、typecheck、Vitest 和 production build；扫描源码与 `dist`，运行 scope、SEM 与 Git 审计；全部通过后把状态更新为 `M10_A_COMPLETE_AWAITING_PROJECT_OWNER_REVIEW` 并停止。

**M10-A 验收命令：**

```powershell
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run
npm --prefix frontend run build
powershell -ExecutionPolicy Bypass -File scripts/dev/check-scope.ps1 -Milestone M10
powershell -ExecutionPolicy Bypass -File scripts/dev/check-sem-integrity.ps1
git diff --check
git diff --cached --check
git status --short --branch
git diff --stat
git diff --name-only
```

预期：TypeScript 0 errors、Vitest 0 failed、production Vite build 成功、`SCOPE_OK M10`、`SEM_INTEGRITY_OK`、暂存区为空；无 Backend、五份设计基线、`SEM/`、M10-B 组件或内部配置 diff；`dist` 不含 Runtime/MinIO/object_key/Secret/绝对路径。

**M10-A 人工验收：** 临时在 `127.0.0.1` 启动 Vite，只验证最小占位页可打开、Vue/CSS 正常、控制台无启动错误且页面无 Secret/内部地址；如 Backend 已运行，只允许通过代理做安全只读 GET。验收后停止 dev server，不留下后台进程。

### M10-B：完整最小界面与交互

**前置条件与范围门：** 只有项目负责人验收 M10-A 并明确批准 M10-B 后才能开始。开始前必须把 `check-scope.ps1` 的 M10 allowlist 从当前 M10-A 范围按本节精确扩展；本轮 M10-A 不提前开放这些路径。

**M10-B 精确 allowlist：**

- 实施管理：本计划、`docs/progress/phase-1-current-status.md`、`scripts/dev/check-scope.ps1`。
- 已有页面入口：`frontend/src/App.vue`、`frontend/src/styles.css`。
- 正式组件：`frontend/src/components/ConversationSidebar.vue`、`frontend/src/components/ConversationList.vue`、`frontend/src/components/TimelineList.vue`、`frontend/src/components/UserMessageItem.vue`、`frontend/src/components/AssistantMessageItem.vue`、`frontend/src/components/ToolTaskCard.vue`、`frontend/src/components/StructuredResult.vue`、`frontend/src/components/AssetGallery.vue`、`frontend/src/components/TaskHistory.vue`、`frontend/src/components/ChatComposer.vue`、`frontend/src/components/GlobalErrorNotice.vue`。
- 组件测试：`frontend/tests/App.test.ts`、`frontend/tests/components/conversation-sidebar.test.ts`、`frontend/tests/components/timeline-list.test.ts`、`frontend/tests/components/tool-task-card.test.ts`、`frontend/tests/components/structured-result.test.ts`、`frontend/tests/components/asset-gallery.test.ts`、`frontend/tests/components/task-history.test.ts`、`frontend/tests/components/chat-composer.test.ts`、`frontend/tests/components/global-error-notice.test.ts`。

M10-B 默认不新增依赖、不修改 M10-A API/composable 行为、不修改 Backend/CORS；如验收测试证明数据层存在实现缺陷，必须先停止并由项目负责人明确扩大精确路径。

**M10-B 功能：** Conversation Sidebar/List、三类 TimelineItem、Tool Task 当前状态与折叠历史、NEEDS_INPUT 明确目标补充、结构化 Result、Asset inline/attachment、Explanation 与最新失败、Tool retry、Explanation retry、Chat Composer、全局安全错误和单轮询状态。不得在组件中重建 Timeline 事实、重排服务端数组、猜测 supplement target 或显示原始 JSON。

**M10-B 验收命令：**

```powershell
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run test -- --run
npm --prefix frontend run build
powershell -ExecutionPolicy Bypass -File scripts/dev/check-scope.ps1 -Milestone M10
powershell -ExecutionPolicy Bypass -File scripts/dev/check-sem-integrity.ps1
git diff --check
git diff --cached --check
```

人工验收使用 Mock Backend 在浏览器完成知识问答、Tool、补参、两类重试、图片查看/下载和安全错误场景；确认重复点击/不确定网络结果复用同 key、旧 Tool 卡不移动、可用部分结果不丢失。M10-B 最终停在项目负责人验收点，未来建议提交：`feat: add minimal chat frontend`；未经批准不得 commit 或开始 M11。

**失败时回退：** 前端保持上一可构建工作单元；Backend API/M9 时间线继续可用，禁止为前端临时绕过 API 所有权、幂等、排序或错误契约。

## M11：阶段 1A 完整 Mock 端到端验收

**总目标：** 在不加载真实模型、不增加产品能力的前提下，把已验收的 PostgreSQL、MinIO、Mock Runtime、Backend 和 Frontend 串成可重复验收的阶段 1A Mock 闭环。M11 拆成两个独立工作单元：M11-A 只负责安全启停与基础旅程；M11-B 才负责故障注入、重放/重试、浏览器验收和正式阶段 1A 报告。M11-A 验收前不得开始 M11-B，M11 整体验收前不得开始 M12。

### M11-A：Mock 栈安全启动/停止与基础端到端旅程

**前置条件：** M10 已由项目负责人验收；开始时 Git 必须位于已验收 M10 commit，暂存区和工作区均为空。实现仍使用 Backend Python 3.11 环境、现有 Mock LLM/Mock Explanation、现有 Mock Runtime、PostgreSQL、MinIO 和 Vite，不安装或升级依赖。

**精确 allowlist：**

- 实施管理：本计划、`docs/progress/phase-1-current-status.md`、`scripts/dev/check-scope.ps1`。
- 开发脚本：`scripts/dev/start-mock-stack.ps1`、`scripts/dev/stop-mock-stack.ps1`。
- 基础 E2E：`backend/tests/e2e/conftest.py`、`backend/tests/e2e/test_mock_journey.py`。

除上述七个路径外不得修改或新增文件；运行时日志和 state 只能写入已忽略的 `tmp/m11-mock-stack/`。不得修改 `.gitignore`、生产 Backend/Frontend/Runtime 代码、migration、五份设计基线、`SEM/`、依赖或正式 PostgreSQL/MinIO 数据。

**安全启动接口与所有权：**

1. `start-mock-stack.ps1` 必须可从任意当前目录运行，依据脚本位置解析仓库根目录；通过可选 `-PythonExecutable` 或本机 Conda 环境发现 Backend Python 的绝对路径，随后所有 Python 子进程都直接使用该解释器，禁止在跟踪文件中硬编码个人绝对路径。
2. 脚本按顺序检查 Python 版本/依赖、根 `.env` 的必需白名单键、本地依赖边界、Docker/Compose、端口 `3000/8000/8100` 和既有 state；只解析已知 `KEY=VALUE`，不执行 `.env`，不输出 Secret。在启动 Docker、执行 Alembic 或 bootstrap bucket 前，必须通过现有 `load_settings()`、`build_postgres_url()`、`parse_minio_config()` 和 `parse_zta35g_runtime_config()` 验证 `POSTGRES_HOST=127.0.0.1`、`POSTGRES_PORT=5432`、`MINIO_ENDPOINT=http://127.0.0.1:9000`、`MINIO_API_PORT=9000`、`MINIO_CONSOLE_PORT=9001`、`MINIO_SECURE=false`、`ZTA35G_RUNTIME_URL=http://127.0.0.1:8100`、非空 `LOCAL_ACTOR_ID` 和有效 `TIMELINE_CURSOR_SIGNING_KEY`；错误只命名配置项，不输出值。
   M11-A 的受控环境不仅用于预检，还必须覆盖 Compose、Alembic、bucket bootstrap 和所有子进程创建，避免调用者 shell 环境覆盖 `.env`。
   Backend、Runtime 和 Frontend 必须使用 role-specific child environment：Backend 接收全部已验证 AppSettings；Runtime 只增加自身 token 和固定 port；Frontend 不接收任何 M11-A Backend 受控字段。Runtime/Frontend 不得继承 PostgreSQL、MinIO、Actor、Timeline 或其他 Backend Secret。
3. Docker Compose 只启动 `postgresql` 和 `minio`，并记录每个服务启动前是否已经运行；只允许停止本轮从未运行变为运行的服务，禁止 `down`、`down -v`、删除 volume、清空正式数据库或 bucket。首次 Compose 查询前必须确认有效 endpoint 是 Windows 本机 named pipe，并记录有效 Docker context 与 engine ID；Docker ownership 由固定 project/file 与本地 engine ID 共同确认，远程 endpoint 或 identity 不一致时不得执行 Compose stop。
4. PostgreSQL/MinIO healthy 后，执行 Alembic 单 head 检查、`upgrade head`、`current` 和 `check`，并用现有 Storage bootstrap 确保配置的正式 bucket 存在；不得清空既有对象。
5. 依次启动 Mock Runtime `127.0.0.1:8100`、Backend `127.0.0.1:8000`、Frontend `127.0.0.1:3000`，分别使用独立 stdout/stderr 日志；成功前必须用有界轮询验证 Runtime live/ready 的 token、contract/tool/bundle 身份，Backend live/ready 的 PostgreSQL/MinIO 状态，以及 Frontend 根页和 `/api/v1/health/live` 代理。
6. `state.json` 采用先临时文件再原子替换，只保存 `schema_version`、`run_id`、repo root、创建时间、Python 路径、Docker engine ID/context、每个进程的 role/PID/start time/executable/command marker/port/log 路径，以及 Compose 服务的 preexisting/owned 标记；不得保存 endpoint、token、密码或完整环境。
7. 已有正常 state 只有在三种固定 process role/marker/port、两个固定 Docker service、ownership 布尔类型、repo root、Docker engine ID/context、PID、start time、固定 wrapper executable、实际命令行和健康状态全部吻合时才返回 `MOCK_STACK_ALREADY_RUNNING`；不得信任 state 自报 marker 或 executable。任何未知/重复 role 或 service、非法布尔值、Docker identity 不一致、陈旧或不完整正常 state 都返回 `STALE_OR_INVALID_MOCK_STACK_STATE`，不得覆盖或接管。无 state 但目标端口被占用时返回 `PORT_ALREADY_IN_USE`，不得杀死未知进程。
8. 任一步失败必须按反向顺序尝试清理本轮已确认拥有的所有进程，并只停止本轮启动的 Compose 服务；每个 native 命令按 exit code 判断，某项失败不阻止其余清理。清理完整时不得遗留 state；清理不完整时必须原子写入只含仍可能需处理资源的部分 recovery state，输出 `MOCK_STACK_START_CLEANUP_INCOMPLETE` 和相对 state 路径，再返回非零。WMI 返回 PID 后必须先建立包含固定 role/marker/port、PID、start time、预期 wrapper 和日志路径的 provisional record，再进行完整 CIM metadata 验证；首次精确 PID 回滚失败时外层 cleanup 必须保留并再次处理该 record，仍不能安全停止时将其写入 recovery state，不得形成无记录孤儿或宽泛杀进程。

**安全停止接口与所有权：**

1. `stop-mock-stack.ps1` 只能读取 `tmp/m11-mock-stack/state.json` 中的精确记录，不得按名称或端口扫描并终止进程。
2. 终止每个进程前按 role 使用脚本固定 marker 和固定 wrapper executable，重新核对 record marker、repo root、PID、start time、port、实际 executable 和实际命令行；未知/重复 role、未知/重复 Docker service 或非法 ownership 布尔值在任何终止动作前整体拒绝。无法证明所有权时输出 `PROCESS_OWNERSHIP_NOT_VERIFIED`，继续执行其余已通过结构校验记录的安全检查，保留 state 并返回非零。
3. 只停止 state 中 `started_by_this_run=true` 的 Compose 服务，且在任何 Compose stop 前重新确认当前 endpoint 仍为本机 named pipe、当前 context/engine ID 与 state 一致；identity 不一致时输出 `DOCKER_OWNERSHIP_NOT_VERIFIED`、保留 state，并继续独立处理可验证的 App 进程。绝不停止启动前已运行的服务；禁止删除 volume 或数据。
4. recovery state 中每种合法 process role 和 Docker service 都允许零或一条记录；stop 必须处理所有实际记录，不要求尚未启动的 role 存在。全部安全停止后删除正常或 recovery state、保留日志并输出 `MOCK_STACK_STOPPED`。无 state 且三个目标端口均空闲时输出 `MOCK_STACK_NOT_RUNNING` 并返回 0；无 state 但任一端口占用时返回非零。

**基础 E2E RED/GREEN：**

1. 先创建 `test_mock_journey.py` 的三条真实 HTTP/数据库/MinIO 旅程，在 session fixture 尚不存在时运行并确认恰因缺少 fixture 失败；不得以导入、语法或生产代码错误充当 RED。
2. 再在 `conftest.py` 实现 session 级本地依赖边界断言、Runtime 身份探针、唯一临时数据库、Alembic upgrade、唯一临时 MinIO bucket、显式 test settings 和 `TestClient`。本地边界断言必须在创建数据库或 bucket 前完成，禁止对远程或非预期 PostgreSQL/MinIO 创建或删除资源。所有清理必须在 `finally` 中关闭 client/engine/pool，删除临时 bucket 中全部对象并删除 bucket，终止临时数据库连接后删除数据库。
3. 知识问答旅程：创建 Conversation，提交普通问题，断言 Task `SUCCEEDED/KNOWLEDGE_QA`、有 AssistantMessage、无 ToolRun/ToolResult/Asset，timeline 只有对应 USER/ASSISTANT 顶层项。
4. 完整 Tool 旅程：提交含现有 Mock 触发词“完整合法 Tool 请求”的消息，断言唯一 ToolRun/ToolResult、`completed_outputs=["sem_image","mechanical_properties"]`、AVAILABLE PNG Asset/ResultAssetLink、Explanation、受控图片内容 PNG magic、Task 与 timeline 只有一个 `TOOL_TASK`、稳定初始消息 anchor；数据库中的 `model_bundle_id` 必须精确为 `mock-zta35g-bundle`，Asset producer 必须是该 Result 的 ToolRun。
5. NEEDS_INPUT 补参旅程：先提交缺 `aging_temperature` 的请求，断言同一 Task 为 NEEDS_INPUT 且无运行/结果/资产；再用新 Idempotency-Key、`SUPPLEMENT_TASK`、原 `target_task_id` 和包含“补充 aging_temperature = 730 °C”的现有 Mock 合法触发文本提交，断言同一 Task 成功、仅创建一个 ToolRun、新旧 Message/Revision 均保留、timeline 仍只有一张 Tool 卡且 anchor 不变。
6. 三条旅程都必须检查公共 JSON/headers 不泄露 `object_key`、bucket、token、完整 traceback、宿主机绝对路径、权重路径或 Runtime 内部载荷。PNG 内容响应还必须断言 `Content-Type=image/png`、`Content-Disposition` 使用不含斜杠、反斜杠、盘符或 repo root 的受控文件名，并保留 PNG magic bytes 检查。

**M11-A 验收命令：**

```powershell
powershell -ExecutionPolicy Bypass -File scripts/dev/start-mock-stack.ps1
<backend-python> -m pytest backend/tests/e2e -q
powershell -ExecutionPolicy Bypass -File scripts/dev/start-mock-stack.ps1
powershell -ExecutionPolicy Bypass -File scripts/dev/stop-mock-stack.ps1
powershell -ExecutionPolicy Bypass -File scripts/dev/stop-mock-stack.ps1
powershell -ExecutionPolicy Bypass -File scripts/dev/start-mock-stack.ps1
<backend-python> -m pytest backend/tests/e2e -q
powershell -ExecutionPolicy Bypass -File scripts/dev/stop-mock-stack.ps1
<backend-python> -m pip check
<backend-python> -m pytest backend/tests -q
<backend-python> -m pytest mock-runtime/tests -q
<backend-python> -m compileall -q backend/src mock-runtime/src
npm --prefix frontend run test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
powershell -ExecutionPolicy Bypass -File scripts/dev/check-scope.ps1 -Milestone M11A
powershell -ExecutionPolicy Bypass -File scripts/dev/check-sem-integrity.ps1
git diff --check
git diff --cached --check
```

预期：首轮启动成功；重复启动输出 `MOCK_STACK_ALREADY_RUNNING` 且不产生第二组进程；停止输出 `MOCK_STACK_STOPPED`，重复停止输出 `MOCK_STACK_NOT_RUNNING`；第二轮启动和三条 E2E 再次成功；最终目标端口无监听、state 已删除、日志保留。全部回归 0 failed，输出 `SCOPE_OK M11A` 和 `SEM_INTEGRITY_OK`，暂存区为空，diff 仅包含七个 allowlist 路径。

**M11-A 人工验收：** 启动后打开 `http://127.0.0.1:3000`，确认页面可加载且前端代理健康；可选只读查看 Runtime/Backend ready 的安全摘要。不得在浏览器直接执行真实模型或把 Runtime 暴露为公共接口。验收后必须安全停止栈。

**M11-A 暂停点：** 更新进度文件时只记录本轮真实命令、测试数量、运行时清理状态和未提交 allowlist diff，并统一标记 `M11A_IMPLEMENTED_AWAITING_PROJECT_OWNER_REVIEW`。未经项目负责人明确验收不得 commit、不得开始 M11-B 或 M12。

### M11-B：故障注入、重放/重试与阶段 1A 正式验收

**前置条件与范围门：** 只有项目负责人验收 M11-A 并明确批准 M11-B 后才能开始；`check-scope.ps1` 的 M11-B allowlist 已按本节预配置，开始 M11-B 时只需对照 Git 实际状态和本节重新核对，不是再次扩展。

**预配置 allowlist：** `docs/progress/phase-1-current-status.md`、`scripts/dev/check-scope.ps1`、`scripts/dev/start-mock-stack.ps1`、`scripts/dev/stop-mock-stack.ps1`、`scripts/acceptance/run-phase-1a.ps1`、`backend/tests/e2e/conftest.py`、`backend/tests/e2e/test_mock_journey.py`、`backend/tests/e2e/test_mock_acceptance_matrix.py`、`docs/acceptance/phase-1a-report.md`。本轮 M11-A 不创建 M11-B 专用脚本、测试或报告。

**功能与验收：** 在复用 M11-A 安全启停和临时资源隔离的前提下，覆盖 DB、MinIO、Runtime、Explanation 故障，同 key 重放、Tool retry、Explanation retry、部分成功、浏览器完整旅程和不含 Secret/图片 payload 的正式阶段 1A 报告。验收脚本最后输出 `PHASE_1A_ACCEPTANCE_PASSED` 并列出每类场景数量与 0 failed。

**失败时回退：** 停止 Mock stack，修复失败所在既有里程碑；不改变已验收数据模型或公共契约来迁就测试，不加载真实权重，不进入 M12。

**M11 完成证据：** M11-A/M11-B 完整命令日志、E2E 和浏览器验收报告、资源计数、scope/SEM/Git 审计。到此必须暂停，等待项目负责人通过 M11 检查点并确认阶段 1A 完成。未来建议提交：`test: add phase 1a mock acceptance`。

## M12：DeepSeek Provider 接入

M12 拆成两个独立暂停点。M12-A 只实现并离线验证 Provider Adapter、应用接线和 Mock 回归；M12-B 才允许使用项目负责人提供的进程环境配置做受控真实 Provider 验收。M12-A 获批前不得开始 M12-B。

### M12-A：离线 Provider 实现、应用接线与 Mock 回归

**目标：** 保留 Mock 自动化替身，新增 DeepSeek Chat Orchestration 与 Explanation Adapter；使用 fake Runnable/model 完成严格 structured output、Prompt digest、usage/request-id 白名单、静态错误矩阵、持久化、幂等和 wiring 的全离线验证。Application 的单位换算、完整性/精度/范围硬校验、Tool Registry 和 Tool 执行权保持不变。

**固定配置：** provider=`deepseek`、model=`deepseek-v4-flash`、根地址 `https://api.deepseek.com`、temperature=0、Chat max tokens=1024、Explanation max tokens=768、timeout=60 秒、SDK retries=0、streaming=false、thinking=disabled、response headers enabled。配置只能接受该精确模型和根地址。

**安全边界：** Key 仅为 `SecretStr | None`，Mock 不要求 Key，DeepSeek wiring 缺 Key fail closed；不保存或记录 Key、Authorization、完整 Prompt、原始响应、完整 headers、reasoning content、SDK 异常正文或内部堆栈。成功只白名单保存 input/output token 和合法 `x-request-id`；失败只保存静态错误码/文案和合法 request id。

**接口与 structured output：** Chat 使用 `with_structured_output(ProviderChatResponse, method="json_mode", include_raw=True)`，Provider Schema 严格禁止额外字段并表达 `KNOWLEDGE_ANSWER | TOOL_EXECUTION | NEEDS_INPUT`。Explanation 使用独立普通 model 实例。每次业务调用最多 invoke 一次，不 repair、不 retry、不 fallback。Provider Schema 通过后仍必须经过现有 Application 硬校验。

**持久化与事务：** Adapter 的纯函数 `request_metadata()` 与实际 invoke 共享同一 Prompt 渲染函数；先提交 PENDING LLMCall，再退出事务调用 Provider，最后用短事务保存白名单 outcome。Prompt template 为 Chat `chat-orchestration/2`、Explanation `tool-result-explanation/2`；仅保存固定身份、canonical SHA-256 digest 和受控 generation parameters。

**自动化验收：** 覆盖三条 Chat route、Explanation、严格 Schema、空响应/非法 JSON/Schema mismatch、400/401/402/422/429/500/503/连接/超时、一次 invoke、双独立 model、显式注入优先、Mock lazy boundary、socket 阻断、Chat 同 key 重放、Explanation retry 同 key 重放与新 key 资源增量。阶段 1A Runner 必须强制 Mock 并精确恢复调用者环境。

**明确不做：** 不发出任何真实 Provider 请求，不读取/设置/索要真实 Key，不创建 live test，不修改本地 `.env`，不修改公共 API Schema、migration、Frontend、Mock Runtime 或 `SEM/`，不运行真实模型。

**暂停点：** 完成离线测试、阶段 1A Mock 回归、Scope、SEM、Secret/Prompt 扫描与 Git 审计后标记 `M12A_IMPLEMENTED_AWAITING_PROJECT_OWNER_REVIEW`。未经项目负责人验收不得暂存、commit、push、amend 或开始 M12-B。

### M12-B：受控真实 Provider 验收

**状态：** `NOT STARTED`。

**前置条件：** M12-A 项目负责人检查点通过；项目负责人另行明确授权真实网络调用和进程环境中的 Key。不得从仓库、`.env`、日志、报告或测试自动发现 Key。

**范围：** 使用 M12-A 已审查 Adapter 对知识回答、完整 Tool 候选、缺参追问、单位候选和 Explanation 做最小真实调用；记录安全摘要、调用计数、错误矩阵和无敏感信息的验收报告。真实 LLM 仍不得直接调用 Tool、Runtime、PostgreSQL 或 MinIO。

**失败时回退：** 配置切回 Mock，保留静态安全失败事实；不修改 ToolResult，不绕过 Application 硬校验，不进入 M13。

**完成证据：** 真实调用安全摘要、三类编排与 Explanation、故障映射、幂等计数、无 Secret/Prompt/raw response 扫描和 Git 审计。到此再次暂停，等待项目负责人通过 M12 overall 检查点。

## 阶段 1B 合并执行工作包

M13–M16 继续作为阶段 1B 的历史能力分解和验收追溯基线；实际执行改为两个合并工作包，避免重复实现已在 M5 落地并经 M11/M12 回归验证的 Backend `LocalZTA35GToolClientAdapter`。

### P1B1：真实 ZTA35G Runtime 离线实现与契约接入

**范围：** 在现有 `materialsagent-backend` 测试环境中，以 Python 3.8 兼容源码、Fake Loader、Fake Model Components 和真实 loopback HTTP 黑盒服务完成 Runtime 配置、严格契约、模型结构提取、bundle 预校验、推理编排、单执行槽、生命周期和现有 Backend Adapter 接线验证。

**历史映射：** 吸收 M13 的 bundle 身份/加载兼容入口、M14 的推理边界与 payload/resource 入口、M15 的 Runtime 离线实现和 Adapter 黑盒契约；不执行这些历史里程碑中的环境创建、依赖安装、真实反序列化、真实推理、GPU 或人工运行验收。

**硬边界：** `SEM/` 只读；四份真实权重不得打开；不得调用真实 `torch.load`、`joblib.load`、模型、GPU、DeepSeek 或根 `.env`；不得修改 Backend 生产代码、Mock Runtime、Frontend、设计基线或 migration。现有 Backend Adapter 只通过新增黑盒测试验证，不再作为实现任务重复开发；若证实其存在缺陷，必须停止并另行评审。

**完成证据：** Runtime unit/contract 全绿；compatibility 入口默认明确授权跳过；现有 Backend Adapter 三种输出、ready/busy、错误与大小边界黑盒通过；Backend、Mock Runtime、Frontend 和阶段 1A 回归通过；`SEM_INTEGRITY_OK`、`SCOPE_OK P1B1`、安全扫描和 Git 审计通过。到此必须暂停，不得进入 P1B2。

### P1B2：真实环境、权重、GPU、Runtime 与浏览器验收

**前置条件：** P1B1 经项目负责人代码审查并另行明确授权；真实模型 Conda 环境、依赖安装、权重打开、GPU、真实 Runtime、浏览器和真实 Provider 调用分别受授权门控制。

**历史映射：** 执行 M13 的 Python 3.8 环境与四权重真实加载、M14 的固定参数最小推理和资源测量、M15 的真实 Runtime 人工运行验收，以及 M16 的真实 LLM + 真实 Runtime 浏览器 E2E。固定 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000` 和全部真实验收暂停点保持不变。

**四门授权方案：**

1. **门一：独立环境与依赖验证。** 项目负责人手工完成
   `materialsagent-zta35g` 候选依赖安装并取得
   `CUDA_BASIC_TEST_PASSED`；Codex 只读核对环境，修订两处测试兼容性后在
   Python 3.8.20 和 Backend Python 3.11.15 完成 Runtime 离线测试，并生成无
   本机绝对路径、包含精确官方 PyTorch CUDA 11.6 wheel 来源和 SHA-256 的依赖锁
   与 Conda 环境记录。Base 原始开始／结束包数组快照没有持久化，不能逐字段复原
   旧哈希差异；日志证明 `d53…` 与 `d446…` 来自不同的数组处理和 JSON 序列化
   算法，而同一算法下的开始／结束 `d446…` 比较结果一致。后续只读诊断确认当前
   Conda 语义投影连续三轮均为
   `7e04c35067f4d257351063091a2d64a79d75c9497f08bf6ff7f008782a3b6d70`
   （505 包），pip 语义投影连续三轮均为
   `69bb3db228283bee065c030f3060c8200dcc2fb20c8879d9e32342d14ab9a937`
   （437 包）；最后 Conda revision 为 `2025-11-14`，本轮无新 revision，
   `conda-meta/history` 和 package JSON 本轮无写入，也无包身份变化证据。结论为
   `BASE_PACKAGE_SET_UNCHANGED` / `AUDIT_FINGERPRINT_FALSE_POSITIVE`。状态为
   `COMPLETE / PROJECT_OWNER_ACCEPTED`。
2. **门二：真实权重加载兼容性。** 打开四份真实权重、调用真实
   `torch.load` / `joblib.load` 并构造模型前必须取得新的项目负责人授权。本轮
   已在 CPU 依次验证 DDPM、DenseNet121、两个 SVR 和正式完整 bundle，只执行加载
   与释放，不执行 forward、predict、transform、DDPM 采样或 CUDA 迁移。
   DenseNet 预比较的 121 个 missing 精确等于全部 121 个旧版 BatchNorm
   `num_batches_tracked` counter；PyTorch 1.13.1 内置兼容逻辑在真正
   `strict=True` 下精确初始化 CPU `int64` 标量 0，strict 返回 0/0。没有忽略
   其他 missing、没有手工填充参数，生产 loader 保持 `strict=True` 且生产源码
   未修改。两个 SVR 的顶层类型、最终 estimator、全部 Pipeline steps、输入维度
   及来源、PCA components 和 support vectors shape 均由固定身份门硬断言通过。
   `zta35g-sem-original-bundle` CPU load/close 和引用释放通过。分阶段 SVR 身份
   检查代码未显式调用 predict、transform、fit 或 score；正式完整 bundle 加载
   路径由运行期 canary 保护，这四类调用计数均为 0。整个真实加载测试同时由
   `Module.__call__` 和 DDPM 动态类/DenseNet/Sequential 直接 forward 两层 canary
   保护，sample/CUDA canary 也全部为 0。
   `ModelBundleLoader` 是无状态工厂；生命周期只以
   `LoadedModelBundle._closed=false → true` 和弱引用清理为证据，不声称存在
   `is_loaded()`。状态为 `COMPLETE / PROJECT_OWNER_ACCEPTED`。
3. **门三：真实 GPU 最小推理与资源测量。** 仅在门二验收和新的 GPU 推理授权后，
   才允许按固定参数执行最小 DDPM/DenseNet/SVR 链路并记录资源事实。固定参数为
   `num_samples=1`、`guide_scale=2.0`、`timesteps=1000`；已按
   A（SEM）、B（性能）、C（两项）和 D（C 的同 seed 重复）完成精确四次
   512×512 / 1000 步采样；共享 Engine 只加载一次，未重试、未尝试第五次，
   RTX 3060 Laptop 6 GiB 可承载，单次完整推理约 8.6–10.7 分钟，
   nvidia-smi peak used 最大约 3093 MiB。payload、资源、重复性和受控复核产物
   均已记录。代码审查修订只补强离线测试
   harness 的加载失败清理、精确 OOM 映射和证据措辞，不重新执行真实 GPU 推理。
   两套解释器的离线 helper 聚焦测试均为 7 passed；清除授权后的默认回归均为
   150 passed、3 个真实 compatibility skip。
   状态为
   `COMPLETE / PROJECT_OWNER_ACCEPTED`。
4. **门四：真实 Runtime 与综合验收。** 仅在前三门分别验收后，才允许启动真实
   Runtime，并执行 Backend、MinIO、真实 Provider、浏览器和阶段 1 综合回归。
   2026-07-30 项目负责人已授权执行；离线读取生产配置后确认
   `ZTA35G_RUNTIME_TIMEOUT_SECONDS` 的现有合法上限为 300 秒，低于门四要求的
   900 秒，命中
   `P1B2_BACKEND_RUNTIME_TIMEOUT_CONFIGURATION_BLOCKED`。未修改生产配置上限，
   未进入 SelfTest、真实 Runtime、综合栈或浏览器阶段。状态为
   `BLOCKED / AWAITING_PROJECT_OWNER_REVIEW`。

   **门四前置生产配置修订：** 2026-07-30 经项目负责人单独授权，只将
   `ZTA35G_RUNTIME_TIMEOUT_SECONDS` 最大合法值从 300 秒精确提高到 900 秒；
   默认 10.0 秒和 `> 0` 最小值规则不变。该上限以门三实测最大约 640.46 秒为
   依据，提供约 259 秒受控余量；当前没有证据支持 1200 秒。既有
   `LocalZTA35GToolClientAdapter` 继续使用 `urllib3.Timeout(total=配置值)`，
   DeepSeek timeout 独立保持默认 60 秒，Runtime 自动重试仍为 0。固定设计决定为
   `0 < ZTA35G_RUNTIME_TIMEOUT_SECONDS <= 900`，默认 10.0 秒；重新执行门四时
   Backend → Runtime timeout 使用 900 秒，外层 execute watchdog 使用 1200 秒，
   DeepSeek timeout 仍为 60 秒。前置修订通过
   TDD 边界、Adapter 精确传递、Backend 全量 Mock 回归与 Frontend 离线回归后，
   项目负责人代码审查结论为
   `P1B2_GATE4_TIMEOUT_PREREQUISITE_CODE_REVIEW: APPROVED`，前置修订状态为
   `COMPLETE / PROJECT_OWNER_ACCEPTED`。不得据此继续门四 A/B/C/D；门四综合
   验收状态为 `NOT STARTED / AWAITING_PROJECT_OWNER_RESTART_AUTHORIZATION`，
   须先形成独立验收提交并恢复干净工作区，再由项目负责人重新授权并从新的正式
   HEAD 完整重新开始，不沿用此前阻塞尝试的执行状态。

   **门四综合验收重新授权尝试：** 2026-07-30 项目负责人从
   `main@0db1c29f661313b10dac81c807f2649210046c1f` 重新授权。完整读取和
   纯离线最小复现确认当前生产语义只允许失败 Explanation 重试：成功
   Explanation 的 Frontend 卡片不显示重试按钮，Backend 也会对成功 Task 的新
   Explanation retry 返回 `EXPLANATION_NOT_RETRYABLE`。这与重新授权方案中
   场景 5“Task SUCCEEDED 且自动 Explanation 成功”后继续执行场景 7“点击重试
   解释”冲突。按生产缺陷停止边界，未创建 Runner/Executor，未进入真实资源阶段，
   真实 Runtime/Provider/DDPM 调用均为 0。门四综合验收状态为
   `BLOCKED / AWAITING_PROJECT_OWNER_REVIEW`，需另行评审成功 Explanation 的
   再生成语义，或把场景 7 明确改为失败 Explanation 的恢复路径。

   **门四最终分阶段资源门修订：** 2026-07-31 项目负责人已把场景 5/7 确认为
   “无效 Key 下自动 Explanation 认证失败，恢复真实 Key 后显式 Explanation
   retry 成功”，并批准只修订资源门和当前 Git 状态规则。当前合法工作区精确为
   4 个 tracked modified、4 个授权 untracked、unexpected 0、staging empty；
   当前实施阶段不要求 `Untracked=0`，不得删除、暂存或提交这 8 个文件。统一
   8 GiB、单点 6.75 GiB、五次采样中位数 6.70 GiB 和 Stage B 6.5 GiB /
   Stage C 6.25 GiB 均为已废止历史规则。Stage B 当前唯一启动门为
   `5905580032 bytes`（5.5 GiB），在设置真实模型授权、启动 Runtime 或打开
   权重前，以 `GlobalMemoryStatusEx.ullAvailPhys` 实际字节判断，失败固定为
   `P1B2_GATE4_STAGE_B_MEMORY_PREFLIGHT_FAILED` 且真实预算保持 0。

   Stage B 必须分开记录 before-runtime-start、after-runtime-ready、pre-execute、
   minimum-during-execute 和 post-execute 实际字节；ready 后无 execute 至少
   等待 2 秒，after-ready 与 pre-execute 各取 3 次、间隔 250 ms 的中位数。
   分别计算 `model_load_drop=max(0,before-start-after-ready)`、
   `execute_drop=max(0,pre-execute-minimum-during-execute)` 和
   `total_drop=max(0,before-start-minimum-during-execute)`。Stage C 只能使用
   本轮 load/execute 两个增量，禁止使用总下降量或门三历史常量。

   场景 1–3 后，浏览器和综合栈已运行、Runtime 尚未启动时，Stage C 启动门为
   `before-runtime-start >= max(4831838208, model_load_drop+1610612736)`，
   失败固定为 `P1B2_GATE4_STAGE_C_RUNTIME_START_MEMORY_FAILED`，不得打开
   权重或增加预算。Runtime ready 且模型已加载后至少等待 2 秒，Tool retry 门为
   `pre-tool-retry >= max(3758096384, execute_drop+1610612736)`；只有通过后
   才输出 `P1B2_GATE4_TOOL_RETRY_RESOURCE_READY`，失败固定为
   `P1B2_GATE4_STAGE_C_TOOL_RETRY_MEMORY_FAILED`，不得发送 retry 或增加
   Runtime/DDPM/Provider 预算。浏览器及综合栈占用已包含在 Stage C 实时读数中。

   运行期低于 2.0 GiB 记录 `HOST_MEMORY_LOW_WARNING` 的首次时间、最低值和
   持续时间；低于 1.5 GiB 记录 `HOST_MEMORY_CRITICAL_LOW`。调用前命中
   critical 不得 delegate；调用中命中则等待返回后立即停止 Runtime、禁止后续
   场景并清理，不得 CPU fallback、降低尺寸/timesteps 或启用 AMP/半精度。
   该规则仅为当前约 15.4 GiB 主机的验收配置，不是生产部署推荐，也不保证其他
   硬件相同。GPU 门和调用预算未降低。当前最终规则离线验证为：PowerShell/
   Python SelfTest 固定成功标记，helper/E2E `65 passed, 1 skipped`，compile、
   Scope、SEM 与 tracked/cached/untracked whitespace 检查均通过。Phase2/
   Stage C 尚未接线并保持 fail closed，不得进入 Tool retry。最终规则下第一次
   Stage B 尝试在 GPU 只读门因清洗环境缺少 NVML DLL 发现所需的普通
   `ProgramFiles`/`ProgramW6432` 路径而停止；补齐这两个非 Secret 系统变量后
   最小诊断和完整离线回归均通过，该次未启动 Runtime/权重且三类预算保持 0。
   第二次尝试 run id `g4b745bf4426744c6653b57b1d` 在模型加载阶段停止，safe
   state 三类预算仍为 0；不打开权重的分层探针进一步定位 Python 二次角色清洗
   遗漏 `USERPROFILE`，单变量恢复后 torch import 与 CUDA 检查正常。Runner 已
   补齐该普通路径且 Key 仍隔离；确认 PID、端口、GPU、Secret 和 `.env` 全部
   干净后精确删除了该 run 临时目录。另以 harmless owned-child 复现确认 cleanup
   误报来自 Windows retained `Popen` handle；Runner 现用
   `GetExitCodeProcess == STILL_ACTIVE` 判活动状态，查询失败继续 fail closed，
   且只把 `ERROR_INVALID_PARAMETER(87)` 视为 PID 不存在；access denied 与其他
   错误保持 indeterminate。ownership/taskkill 与错误注入回归均已通过。
   第三次 Stage B 尝试 run id `g42ecb990e686728656bdf99e9` 仍在 model loading
   阶段安全失败且三类预算保持 0；根因是 Runner 将 `ZTA35G_MODEL_ROOT` 错指向
   `SEM/`，而 manifest 确认的精确 bundle root 是 `SEM/ZTA35G_lab/`。离线
   RED/GREEN 已修正根目录并完成 Secret/端口/GPU/`.env`/临时目录清理。按三次
   失败审计边界，本轮不执行第四次真实尝试，等待项目负责人复核。

   **第四次且最终一次 Stage B：** 项目负责人随后单独授权本次 Runtime-only
   尝试。run id `g452f78ce7e468dea983977272` 的 Runtime PID `5432` 从精确
   bundle root 加载模型到 cuda，只监听 `127.0.0.1:8100`。Token/非法请求
   负测未增加 delegate；唯一 combined execute 用时 `632.797 s` 并返回
   `SUCCEEDED`，并发合法请求以 `503 / RUNTIME_BUSY / retryable=true` 拒绝。
   Runtime/DDPM/Provider 最终预算为 `1/2`、`1/2`、`0/5`。模型加载、推理和
   总内存下降分别为 `1453727744`、`2704375808`、`4138758144 bytes`，最低
   主机可用内存 `2376253440 bytes`，GPU peak used 3085 MiB；warning 和
   critical 均未触发。Runtime、端口、GPU、临时目录和环境均已完整清理。

   执行进程已验证 Base64 NPY 的 hash、`allow_pickle=False`、`<f4`、
   `[512,512]`、C contiguous、finite和值域边界，性能值为
   `413.39765052163557 MPa` 与 `2.9387915447083017 %`。但现有安全 summary
   未持久化精确 NPY/Base64/JSON 字节数、图片 SHA-256、实际 min/max，且未
   显式保存非全常数证据；强制清理后无法在不执行被禁止的第二次 execute 的
   前提下补采。相同固定输入、seed、bundle 和主机环境已经在门三独立验证这些
   字段；项目负责人接受其为非阻塞证据限制，并决定不消耗第二次 Runtime 预算
   重复 Stage B。Stage B 状态为 `COMPLETE / PROJECT_OWNER_ACCEPTED`，第五次
   Stage B `NOT AUTHORIZED`。当时的 Stage C 授权在后述 Provider 隔离事故后
   已整体作废；Runtime `1/2`、DDPM `1/2` 仍是 Stage B 历史事实，Provider
   `0/5` 不具权威性，不得据此继续 Backend Tool retry。

   **Stage C 初始综合栈执行：** Stage C run id
   `g456aaa9615b3b4bd08bd1d9e9` 原计划从 Runtime `1/2`、DDPM `1/2`、
   Provider `0/5` 快照开始；事故后该 Provider 快照已失效。离线 RED/GREEN 补齐预算恢复与跨 Backend/Runner
   重启不归零；最终 SelfTest、helper/E2E `71 passed, 1 skipped`、compile、
   Scope、SEM 和 Git checks 通过。

   Phase1 先后暴露三项 Runner 接线缺陷：PowerShell 二次清洗遗漏
   `PROGRAMFILES`/`PROGRAMW6432` 导致 Compose 不可发现；Frontend
   PowerShell `-Command` 拆分含空格的 Node 路径；Phase1 回滚沿用 start-time
   存在判断而误报 retained handle。三项均以单变量诊断和 RED/GREEN 修复，
   Secret 角色隔离未放宽。

   完整 Phase1 仍固定命中 `P1B2_GATE4_PROCESS_IDENTITY_FAILED`；Compose、
   Backend、Vite 的单独边界均已通过，但集成 ownership 失败未能在批准边界内
   进一步收敛。按系统化调试停止条件不再重复启动。浏览器 marker 未输出，真实
   Runtime 始终停止，Provider/Runtime/DDPM 无新增调用。临时 database/bucket
   已确认不存在，命名 volume fingerprint 不变，全部目标端口、GPU 和 `.env`
   恢复。该次 Stage C 后续因 Provider 预算事故整体作废；现行状态为
   `BLOCKED / SAFETY_REMEDIATION_REQUIRED`，事故记录标记为
   `P1B2_GATE4_PROVIDER_BUDGET_INCIDENT_RECORDED`。

   **清理后 Mock 回归事故：** 首次手工 Backend 回归没有在测试子进程显式覆盖
   `LLM_ADAPTER=mock`，而根 `.env` 配置为 `deepseek`。单文件诊断明确观察到
   至少 8 个本应为 Mock 503 的请求返回 200；此前同环境 Backend full 还运行了
   300 秒后才终止。临时数据库已清理，无法精确恢复实际 Provider delegate
   总数。Runner 持久账本仍为 Provider `0/5`，但它不再代表实际预算；至少
   存在 8 次真实 Chat delegate，Provider 预算不可审计且不合规。显式强制
   Mock 后的 fresh 回归为 Backend full `1063 passed, 1 skipped`、Mock
   Runtime `11 passed`、Frontend `204 passed`，两套 pip/compile、
   Frontend typecheck/build 与 Alembic head/current/check 均通过；这些通过
   结果不消除预算事故。Stage C 继续保持 `P1B2_GATE4_BLOCKED`。

**Provider 隔离事故发生后的历史状态（已由下文安全整改附录取代）：** P1B2 门一
`COMPLETE / PROJECT_OWNER_ACCEPTED`；P1B2 门二
`COMPLETE / PROJECT_OWNER_ACCEPTED`；门三
`COMPLETE / PROJECT_OWNER_ACCEPTED`；门四 timeout 前置修订
`COMPLETE / PROJECT_OWNER_ACCEPTED`；门四 Stage B
`COMPLETE / PROJECT_OWNER_ACCEPTED`；门四综合验收
`BLOCKED / SAFETY_REMEDIATION_REQUIRED`。

## M13：Python 3.8 模型环境与权重加载验证

**目标：** 建立隔离的 `materialsagent-zta35g` 环境，只读核验模型文件 SHA-256，加载 DDPM、DenseNet121 和两个 SVR，形成兼容性验收记录。

**用户价值：** 在写真实 Runtime 前先证明旧模型文件能被当前硬件和依赖组合识别，失败可独立定位而不影响阶段 1A。

**前置条件：** M12 项目负责人检查点通过；负责人允许进入阶段 1B；`SEM/` 只读；G0 manifest/完整性脚本仍为当前基线。

**允许修改的文件范围：** `environments/materialsagent-zta35g.yml`、`zta35g-runtime/requirements-win-py38.lock.txt`、`zta35g-runtime/compat/**`、兼容性 tests、`docs/acceptance/zta35g-compatibility.md`。只读访问 `SEM/`。

**明确不做：** 不修改/格式化/复制覆盖 `SEM/` 文件，不运行完整推理，不实现 HTTP Runtime，不把旧依赖装进 Backend。

**实现步骤：**

1. 以设计基线记录的版本作为候选创建 Python 3.8 环境并记录 `conda list`、Python/GPU/驱动信息；验证前不得把候选版本称为已锁定版本。
2. PowerShell `Get-FileHash -Algorithm SHA256` 记录四个权重身份，bundle id 固定 `zta35g-sem-original-bundle`。
3. 编写只读 load probe，分别加载 DDPM、DenseNet121、两个 SVR；记录 missing/unexpected keys 和兼容转换。
4. 每加载一项立即释放临时对象；记录启动/加载耗时与峰值内存/显存，不进行 DDPM 采样；验证成功后从实际环境生成精确依赖记录和可复现安装说明。

**自动化测试与四类场景：** 成功四权重加载；输入错误 hash/文件缺失得到明确失败；依赖失败 CUDA/库版本不兼容安全记录；重复 load probe 不改权重且 SHA-256 前后相同。

运行：`conda env create -f environments/materialsagent-zta35g.yml`；`conda run -n materialsagent-zta35g python -m pytest zta35g-runtime/tests/compatibility/test_model_loading.py -q -s`

预期：Python 3.8.x；四个 hash 已记录；测试给出每个模型 PASS/FAIL、missing/unexpected keys，不把 traceback 写进公共响应。

**人工验收步骤：** 项目负责人查看兼容性表和每个权重结论；确认 `git status --short -- SEM/` 与开始前完全一致。

**失败时回退：** 删除独立 Conda 环境和 probe 生成的临时缓存；保留失败报告；不得修改权重或阶段 1A Backend。

**完成证据：** 实际精确依赖记录、环境导出、四个 SHA-256、加载日志安全摘要、显存记录、G0 SEM manifest 前后检查、diff 检查。到此必须暂停，等待项目负责人通过 M13 检查点。未来建议提交：`test: verify zta35g model loading compatibility`。

## M14：真实模型最小推理与测量

**目标：** 用固定配置执行一条最小 SEM 生成和完整性能预测，验证图像/数值、耗时、显存、并发 1 和 Base64 `.npy`。

**用户价值：** 证明模型不仅能加载，还能在当前实际验收主机 RTX 3060 Laptop GPU / 6144 MiB 上产生可检查结果，并为 Runtime 超时提供数据。真实推理是否可承载必须由门三实测，不得依据旧 8 GB 候选硬件推断。

**前置条件：** M13 项目负责人检查点通过。

**允许修改的文件范围：** `zta35g-runtime/compat/**`、compatibility tests、`docs/acceptance/zta35g-inference.md`、受控输出目录的 `.gitignore`。`SEM/` 只读。

**明确不做：** 不实现 HTTP 服务，不调整固定参数，不重构模型，不宣称单样本基准等于生产 SLA。

**固定输入：** 使用基线范围内一组明确四维参数，seed 固定为验收记录值；`num_samples=1`、`guide_scale=2.0`、`timesteps=1000`。

**实现步骤：**

1. 先验证固定配置能否运行；若源码要求不同，停止并先更新兼容性记录/设计基线，不静默换值。
2. 运行单张 SEM，检查 float32、`[512,512]`、二维、有限值、实际 min/max。
3. 用同一原始图像运行 DenseNet+两个 SVR，记录性能结构和安全 warning。
4. 分开记录模型冷启动加载、第一次推理、warm-up 和每次 warm 推理原始耗时；分别记录 SEM 生成、性能预测、`.npy` 编码与 Base64 开销，以及 GPU 显存峰值和主机内存峰值；执行两个并发探针验证只允许 1。
5. 编码/解码 Base64 `.npy`，验证 `allow_pickle=False`、大小与 4 MiB 余量。

**真实性能统计规则：** 每组统计必须报告 `sample_count`、minimum、maximum、mean、median/P50，并保留 warm 推理逐次原始耗时。P95/P99 只在样本数足以支持对应分位数且报告说明计算方法时给出；样本不足时明确标记 `insufficient_samples`。不得为了产生 P99 强制执行大量昂贵 DDPM 推理。

**自动化测试与四类场景：** 成功完整推理；输入错误越界/固定参数不一致在模型前拒绝；依赖失败 OOM/CUDA错误安全记录；相同 seed 重跑记录可复现程度，不把重试当成同 ToolRun。

运行：`conda run -n materialsagent-zta35g python -m pytest zta35g-runtime/tests/compatibility/test_minimal_inference.py zta35g-runtime/tests/compatibility/test_payload_and_resources.py -q -s`

预期：测试明确报告固定配置是否 PASS；图像无 NaN/Inf；`.npy` 小于响应上限；所有必需分项和基础统计有实际记录，不预设秒数；样本不足的 P95/P99 标记 `insufficient_samples`。

**人工验收步骤：** 查看由 Backend 同规则离线编码的验收 PNG和性能结果；项目负责人判断结果是否合理并审阅 warning。

**失败时回退：** 保留失败证据并停止 M15；删除受控临时输出，不改 `SEM/`。若固定配置不兼容，走设计变更门而非代码绕过。

**完成证据：** 最小推理记录、图片元数据、性能值、必需基础统计、warm 原始耗时、冷启动/首次/warm-up/分项开销、显存/主机内存、Base64 大小、G0 SEM manifest 前后检查、diff 检查。到此必须暂停，等待项目负责人通过 M14 检查点。未来建议提交：`test: validate zta35g minimal inference`。

## M15：真实 Runtime 与现有 Local Tool Client Adapter 验收（历史分解）

**目标：** 在 Python 3.8 环境实现只监听 loopback、共享 Token 保护、加载一次复用、并发 1 的真实 `/internal/v1` Runtime，并让 Backend Adapter 可配置切换 Mock/真实。

**用户价值：** 平台第一次通过正式内部契约调用真实模型，但公共 API、数据模型和前端不改变。

**前置条件：** M14 项目负责人检查点通过；M14 已给出真实超时与资源数据。

**允许修改的文件范围：** `zta35g-runtime/src/**`、Runtime contract tests、Backend `infrastructure/tool_clients/local_zta35g.py` 与配置、M15 integration tests、`docs/acceptance/zta35g-runtime-runbook.md`。

**明确不做：** Backend 不启动/重启/守护 Runtime；Runtime 不访问 DB/MinIO/LangChain，不自动重试/排队/取消，不返回 PNG。

**接口：** live/ready/execute 全部要求 `X-ZTA35G-Runtime-Token`；模型启动加载一次；固定参数严格核对；busy 立即 503 + retryable true；响应 Base64 `.npy`。

**实现步骤：**

1. 先用 Mock contract suite 对真实 Runtime 做黑盒红测。
2. 实现配置/Token安全比较、生命周期、健康状态、单执行槽和安全错误。
3. 接入 M13/M14 已验证加载/推理函数，不改变公共模型行为。
4. 使用现有 Backend Local Adapter 验证 ID/版本/大小/`.npy`/错误校验和 execute 无自动重试；不得重复实现 Adapter，发现真实缺陷时停止并另行评审。
5. 按“手动 Runtime→ready→Backend”写最小运行手册。

**自动化测试与四类场景：** 成功真实 execute；输入错误 Token/参数/version/payload 被拒；依赖失败进程崩溃/超时/模型加载失败映射；公共同 key 重放不再次调用，显式 retry 新 ID/seed。

运行：`conda run -n materialsagent-zta35g python -m pytest zta35g-runtime/tests/contract -q -s`；`conda run -n materialsagent-backend python -m pytest backend/tests/contract/test_real_runtime_adapter.py -q -s`

预期：两套契约测试 0 failed；端口只在 127.0.0.1；logs 无 Token/图片 payload/权重路径；execute 调用无隐式重试。

**人工验收步骤：** 手动启动 Runtime，带 Token检查 ready，再启动 Backend；错误 Token、第二并发、超时和正常请求各执行一次。

**失败时回退：** Backend 配置切回 Mock Adapter；停止真实 Runtime；阶段 1A 全链路保持可用，真实模型文件不改动。

**完成证据：** 监听地址、ready 响应、Token负测、busy并发、超时映射、模型加载一次日志、Adapter契约结果、diff 检查。未来建议提交：`feat: add real local zta35g runtime adapter`。

## M16：真实 LLM 与真实 ZTA35G Runtime 完整端到端验收

**目标：** 同时启用 M12 的真实 LangChain/LLM Provider 和 M15 的真实 ZTA35G Runtime，完成公共消息提交到 Vue 时间线的最终全链路和运行手册；最终验收不得继续使用 Mock LLM。

**用户价值：** 项目负责人能够从真实用户入口得到正式 PNG、结构化性能和解释，并确认失败/重试/幂等仍符合产品设计。

**前置条件：** M12 的真实 LLM 检查点与 M15 均通过；真实 Provider Key 只由环境变量提供；项目负责人允许执行最终真实 E2E。

**允许修改的文件范围：** real E2E tests、`scripts/acceptance/run-phase-1b.ps1`、`docs/acceptance/phase-1b-report.md`、最小运行手册；只允许为发现的契约实现缺陷修改明确所属模块。

**明确不做：** 不扩展新功能，不优化成远程 GPU，不加入自动启动/队列/SSE/登录/多 Tool，不修改 `SEM/`。

**实现步骤：**

1. 按手册手动启动 Runtime→ready→Backend→Frontend，并明确把 Chat Orchestration 与 Explanation 配置为真实 LangChain/Provider Adapter、Tool Client 配置为真实 Runtime Adapter。
2. 使用真实 LLM 验证 KNOWLEDGE_ANSWER、TOOL_EXECUTION、NEEDS_INPUT、四维参数/单位/requested_outputs 提取和真实 Explanation；Tool 路径完成三种 requested_outputs、部分成功、解释超时、Tool retry、Explanation retry、断开重放。
3. 核对 Asset PNG、ToolResult provenance、timeline anchor、日志三 ID 和公共安全过滤。
4. 汇总模型冷启动、第一次推理、warm-up、warm 逐次原始耗时、SEM 生成、性能预测、`.npy`/Base64 开销、GPU 显存峰值、主机内存峰值，以及 `sample_count/minimum/maximum/mean/median(P50)`；P95/P99 样本不足时标记 `insufficient_samples`，不得为分位数强制大量昂贵推理。

**自动化测试与四类场景：** 成功覆盖真实知识回答、真实 Tool 路由、三种真实输出和真实 Explanation；输入错误覆盖真实 NEEDS_INPUT、缺参/越界/固定参数不可调；依赖失败覆盖 Provider 认证/限流/超时/非法 structured output、Runtime busy/timeout/crash、MinIO/Explanation 失败；同 key 重放与两类显式重试资源计数正确，真实 LLM 不直接执行 Tool。

运行：`powershell -ExecutionPolicy Bypass -File scripts/acceptance/run-phase-1b.ps1`

预期：脚本输出 `PHASE_1B_ACCEPTANCE_PASSED`；所有契约/E2E 0 failed；证据明确显示 Chat/Explanation 使用真实 Provider、Tool 使用真实 Runtime；报告列出真实性能与资源数据，而不是“测试通过”四个字。

**人工验收步骤：** 项目负责人在浏览器完成真实请求、查看/下载图片、比较重试、检查旧卡位置，并审阅兼容性和运行手册。

**失败时回退：** 分别切回 Mock LLM Adapter 和 Mock Runtime Adapter，保留安全的真实失败 LLMCall/ToolRun/日志/报告；不覆盖旧成功结果、不修改权重、不进入生产部署。

**完成证据：** 真实 LLM + 真实 Runtime E2E 命令日志、API/界面截图、Provider/Runtime Adapter 配置安全摘要、Asset SHA-256、LLMCall/Runtime/Backend 关联日志、合规性能/资源表、错误/重试资源计数、五份基线一致性检查、G0 SEM 完整性检查和 `git diff --check`。到此必须暂停，等待项目负责人通过 M16 最终检查点。未来建议提交：`test: complete phase 1 real capability acceptance`。

---

## 每个未来执行对话的固定收尾模板

每个里程碑或里程碑内拆分对话在结束前必须运行并汇报：

```powershell
git status --short --branch
git diff --check
git diff --stat
git diff --name-only
```

随后运行该工作单元列出的局部测试。若修改 Backend 公共契约，还要运行全部 `backend/tests/contract`；若修改迁移，运行 `alembic upgrade head` 并检查单一 head；若修改 Runtime 契约，同时运行 Mock 与真实 Runtime contract suite；若修改前端 API 类型，运行前端 test 和 production build；若工作涉及 `SEM/` 或阶段 1B，运行 `scripts/dev/check-sem-integrity.ps1`。

汇报必须包含：目标是否达到、四类场景实际结果、命令与退出码、人工验收证据、允许范围之外是否有 diff、回退是否可用、是否到达暂停点。结束前更新 `docs/progress/phase-1-current-status.md`，未经项目负责人验收不得 commit 或进入下一工作单元。不得只写“测试通过”。

## 计划执行位置

本文件不在末尾固定当前执行到哪个里程碑。任何新 Codex 对话都必须先读取 `docs/progress/phase-1-current-status.md`，并以其中记录的当前 branch、HEAD、工作单元、状态和下一步为准。未经项目负责人验收，不得进入下一工作单元或里程碑。

## P1B2 门四 Stage C 安全整改附录（2026-07-31）

本附录覆盖门四 Stage C 当前安全边界，优先于上文历史 M16 执行步骤：

- P1B2 门四 Stage C 安全整改为 `COMPLETE / PROJECT_OWNER_ACCEPTED`。项目负责人
  最终代码审查结论为：

  ```text
  P1B2_GATE4_STAGE_C_SAFETY_REMEDIATION_CODE_REVIEW:
  APPROVED
  ```
- Stage B 为 `COMPLETE / PROJECT_OWNER_ACCEPTED`，第五次 Stage B
  `NOT AUTHORIZED`。
- 当前 Stage C run 因 Mock Provider 隔离事故永久为
  `INVALIDATED / PROVIDER_BUDGET_UNAUDITABLE`；已知计划外真实 Chat delegate
  下界为 `>= 8`，精确总数不可恢复，旧 Runner Provider `0/5` 不具权威性。
- 新的真实 Stage C 为
  `NOT STARTED / AWAITING_PROJECT_OWNER_AUTHORIZATION`。除受控完整 Mock 回归
  可复用 Phase 1A 的 owned Mock 栈外，不得启动 Docker、Backend、Frontend、
  Runtime、数据库、MinIO 或浏览器，也不得打开权重、创建 CUDA Tensor 或调用
  Provider。
- 所有 Mock 回归必须通过受控 Runner；精确子进程环境显式强制
  `LLM_ADAPTER=mock`、空白覆盖 `DEEPSEEK_API_KEY`，移除全部真实调用授权，并在
  test collection 前验证 Mock settings、DeepSeek Adapter 构造 0 和 Provider
  delegate 0。
- 代码审查后将入口拆成 `MockHelperTests` 与 `MockFullRegression`；
  `MockRegression` 兼容名必须映射到完整回归。完整回归复用
  `scripts/acceptance/run-phase-1a.ps1`，在系统临时目录从固定 HEAD 创建只含
  已跟踪文件的干净快照，不复制或链接根 `.env`，不修改真实 index/working
  tree；临时配置只白名单读取 Phase 1A 非 Provider 基础设施字段，并强制 Mock
  adapter、空 Provider Key 和空全部真实授权。
- Provider 调用预算必须在真实 Adapter 外层由独立文件账本保护，并通过现有
  `create_app()` 显式 Chat/Explanation port injection 接线。账本在 delegate 前
  加锁、校验 run 与预算、先增加 attempt、fsync 临时文件、原子替换、重新读取
  并与当前 run 的 `LLMCall` 数据库事实核对。三次 Backend 重启共享同一账本。
- 账本只允许 `EXACT/UNKNOWN/INVALIDATED`；缺失、损坏、run 不匹配、字段缺失、
  计数回退、数据库冲突、写入/读回失败均固定
  `P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN`，禁止后续真实调用。
- Provider 账本是 delegate 边界的权威保守计数：无崩溃流程中应与 DeepSeek
  `LLMCall` 事实一致；attempt 预留后、Adapter 调用前异常退出时可以保守多计，
  但不得少计或放行超预算调用，run 必须转为 `UNKNOWN/INVALIDATED`，不得写
  `PASSED`。
- 进程 ownership 必须同时验证 role/port、PID、start time、完整 executable
  path SHA-256、`command_argument_count`、`command_arguments_sha256`、精确
  command marker 和 port owner。参数摘要必须对保持顺序和完整 token 的
  `argv[1:]` 进行 UTF-8 紧凑 JSON 数组序列化并计算 SHA-256；marker 不能代替
  完整参数身份。role 由 record schema、精确 argv 和 marker 共同约束，port 的
  独立证据是实际 LISTENING 与 owner PID。安全诊断不得包含完整命令行、环境或
  Secret。
- Phase1 启动任一步失败必须原子回滚本轮 owned processes、database、bucket、
  ports 和环境，并把 run 标为 INVALIDATED；不得恢复成可沿用状态。未来重新
  授权后必须生成不同 run id、ledger、database、bucket、Actor 和 Conversation。
- 完整 Mock 回归在发送 start 命令前即承担 cleanup responsibility；start
  非零、超时或缺 marker 也必须调用幂等 stop，并核对
  3000/8000/8100、5432/9000/9001 和固定 `materialsagent` Compose 容器集合。
  基线存在目标 listener/container 时启动前 fail closed，不得接管未知资源。
- 代码审查修订验证：受控 helper `117 passed, 1 skipped`；完整
  `MockFullRegression` 复用 Phase 1A，run id
  `20260731T064439Z-c941e3f9aa0b`，Backend E2E `24 passed`、Backend full
  `992 passed`、Mock Runtime `11 passed`、Frontend `204 passed`，typecheck、
  build、Alembic、pip/compileall、SEM/M12A Scope、Secret 扫描与 cleanup
  通过；DeepSeek `LLMCall` 增量和 Adapter 构造均为 0。
- 干净快照及动态产物在成功清理后删除，最终审查包不保留原始 Phase 1A 动态
  文件；保留的是受控 Runner 从实际日志解析、校验后输出的安全摘要。
- Stage B 真实 Runtime 事实继续有效；新的 Stage C 不得执行第五次 Stage B，
  不得复用旧 Provider 账本。完整 Mock 回归只能通过受控 Runner；任何 Provider
  账本 `UNKNOWN/INVALIDATED` 状态均不得继续。

事故记录固定标记：
`P1B2_GATE4_PROVIDER_BUDGET_INCIDENT_RECORDED`。

下一步固定为：创建安全整改唯一验收提交并恢复干净工作区。项目负责人重新授权
后，使用全新的 `run_id`、Provider 账本、database、bucket、Actor 和
Conversation 执行 Stage C 核心真实浏览器闭环；不得恢复或复用污染 run。

## P1B2 门四-A 前置 Chat Orchestration Harness 完善（2026-08-03）

本工作单元由项目负责人从干净基线
`main@b074a89563e94bf18419d5bff865636b71326885` 独立授权。它只修复真实浏览器
体验已经暴露的 Chat Harness 输出意图遗漏，不授权新的真实 Stage C、真实
DeepSeek、真实 Runtime、GPU、权重或 `.env` 操作。

**根因与最小架构方案：** 当前 Chat 使用 `json_mode`，Provider 实际只接收
messages 与 JSON response format，不接收 Pydantic 字段 description；旧 system
message 的 Tool/NEEDS_INPUT 示例又都只包含 `requested_outputs=["sem_image"]`，没有
完整解释普通用户用语、并列请求、明确排除与中间 SEM 的交付语义。Tool Registry
已知的能力和限制也没有进入模型上下文。Application 会确定性校验和去重，
但不允许重新猜测 LLM 已遗漏的输出。修订只在 DeepSeek Chat Adapter 的单次调用
Harness 内补齐以下内容：

1. 明确唯一 Tool 能生成 SEM 图像，并能预测由屈服强度和延伸率组成的力学性能；
2. 明确 `requested_outputs` 只表示用户要求的交付项，不表示 Tool 内部计算；
3. 对只图像、只性能、图像与性能、自然表达、明确排除和缺参场景提供对称说明；
4. 将 route、Tool、material、四维候选参数、输出集合与追问字段的 JSON 合同直接
   写入 Provider 实际接收的 system message；Pydantic Schema 只负责本地解析校验，
   不把 `json_mode` 下不会发送给 Provider 的字段 description 当作 Prompt；
5. 缺参时保留已经理解的输出意图并返回 `NEEDS_INPUT`，不得猜参数；
6. 保持一次 `CHAT_ORCHESTRATION` LLM 调用，不增加规则路由、关键词补丁、judge、
   repair、retry、fallback 或新的 Application 决策。

**精确允许路径：**

- `backend/src/materialsagent/infrastructure/llm/deepseek_chat.py`
- `backend/tests/contract/test_deepseek_adapters.py`
- `backend/tests/contract/test_chat_orchestration_harness.py`
- `scripts/dev/check-scope.ps1`
- `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`
- `docs/progress/phase-1-current-status.md`
- `docs/acceptance/phase-1b-report.md`

**TDD 与验收顺序：**

1. 先建立离线合同矩阵，验证 Provider 可见 messages、本地 Pydantic 严格解析、
   route/domain 映射和单次 invoke；在生产 Harness 未修订时保存预期 RED；
2. 最小修订 Chat system message 内的 Tool 描述、结构化输出说明和模板版本，运行
   聚焦 GREEN；不切换既有 `json_mode`；
3. 运行 DeepSeek Adapter 合同、Chat Orchestration 相关单元/合同回归、Backend
   全量 Mock 回归、Mock Runtime 全量回归、`compileall`、`pip check`、Scope、SEM
   与 Git whitespace 检查；所有 Mock 子进程必须显式强制 `LLM_ADAPTER=mock`、
   空 Provider Key 和空真实授权，不读取根 `.env`；
4. 自审最终 diff，删除不能直接支撑输出意图完整性的抽象、规则或文档扩张；更新
   当前进度和 Phase 1B 验收报告。

离线 fake Runnable 直接回放预置 payload，只证明 Provider 可见消息的组成、本地
Pydantic 解析、Adapter/domain 映射和一次调用合同；即使输入采用自然语言样例，也
不得写成真实 DeepSeek 自然语言理解或 Prompt 质量通过证据。真实 Provider Prompt
质量评测需要后续单独授权。完整回归不得启动真实 Runtime 或 GPU。结束时保持
staging empty，不 push、不 amend，并停在本工作单元边界；不得据此进入新的真实
Stage C。

独立复审结论为 `APPROVED`，Critical、Important、Minor 均为 0；项目负责人据此
验收，本工作单元状态为 `COMPLETE / PROJECT_OWNER_ACCEPTED`。验收收尾只允许精确
暂存上述 7 个路径并创建一个由 Git 生成 hash 的唯一验收提交；提交后必须恢复
working tree clean、staging empty、untracked 0 并停止。该验收不改变离线证据边界：
真实 DeepSeek Prompt 质量仍未验证，也未授权真实 Provider、Runtime 或 GPU。
