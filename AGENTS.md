# Materials Agent — Coding Agent Rules

本文件只规定 Coding Agent 在本仓库中的工作方式、安全边界和验证义务。项目用途、启动方式与
当前能力见 `README.md`；稳定架构、Tool 机制、状态模型与 Runtime 边界见
`docs/project-context.md`。不要把本文件扩写成第二份项目技术文档。

## 事实来源

处理任务时按以下顺序判断事实：

1. 项目负责人当前明确指令；
2. 当前代码、配置、数据库迁移、测试和实际运行行为；
3. 根 `README.md` 中面向使用者的稳定项目说明；
4. `docs/project-context.md` 中经当前实现确认的长期架构上下文；
5. Git 历史中的旧设计决定、实施计划和验收记录。

代码、测试、运行行为或文档彼此冲突时，先查明差异并报告。不得仅为通过测试而改变产品语义，
也不得把历史文档当成现役合同。需要历史背景时使用 `git log -- <path>` 和
`git show <commit>:<path>`，不要把旧文档恢复到当前树中冒充当前事实。

## Repository Navigation

| 路径 | Agent 应在这里查找什么 |
|---|---|
| `backend/src/materialsagent/api/` | FastAPI 路由、依赖注入和公共 schema |
| `backend/src/materialsagent/application/` | Agent Runtime、Tool Registry、参数解析、任务、执行与最终回答 |
| `backend/src/materialsagent/domain/` | 领域模型、状态约束和端口合同 |
| `backend/src/materialsagent/infrastructure/` | PostgreSQL、MinIO、LLM 和 Runtime 适配器 |
| `backend/alembic/versions/` | 数据库迁移；持久化结构变化必须与领域和测试一起核对 |
| `backend/tests/` | unit、api、contract、integration 和 e2e 测试 |
| `frontend/src/api/` | API 客户端和 TypeScript 公共类型 |
| `frontend/src/components/`、`frontend/src/composables/` | Vue UI、交互流程和轮询/幂等逻辑 |
| `frontend/tests/` | 前端 API、组件、composable 和应用流程测试 |
| `mock-runtime/` | Python 3.11 的契约一致 Mock Runtime 及测试 |
| `zta35g-runtime/` | Python 3.8 的真实 ZTA35G Runtime 适配层和隔离测试 |
| `environments/` | Backend 与真实 Runtime 的 Conda 环境声明 |
| `scripts/dev/` | 本地栈、范围检查和 SEM 完整性脚本 |
| `scripts/acceptance/` | 离线/本地验收入口与精确 allowlist |
| `docs/project-context.md` | 长期架构、数据流、Tool 与状态模型的权威上下文 |
| `docs/acceptance/sem-package-manifest.json` | 外部 `SEM/` 包的文件大小与 SHA-256 基线 |
| `SEM/` | 本机只读外部模型包；可能不在普通 worktree 中，且不进入 Git |
| `tmp/` | 被忽略的本地运行状态、日志和验收输出；不是项目事实来源 |

## Before Editing

1. 完整读取本文件，并按任务需要读取 `README.md` 或 `docs/project-context.md` 的相关章节。
2. 检查当前 branch、HEAD、暂存区、未暂存和未跟踪文件；工作区可能已有用户改动。
3. 确定受影响模块，读取相关实现、配置、迁移、公共合同和测试，不能只从文档或文件名推断。
4. 检查目标文件是否与已有改动重叠；保留用户改动，不回退、不覆盖、不顺带整理无关内容。
5. 做满足任务所需的最小改动。行为变化必须同步测试；缺陷修复应在可行范围内增加回归测试。
6. 运行与改动风险相称的验证，检查最终 diff 和 Git 状态，再如实报告未覆盖风险。

遇到跨范围需求或行为语义不清时，先从实现和测试取证。若选择会改变产品或架构语义，停止并
请项目负责人确认，不得替负责人扩大范围。

## 安全与架构硬边界

- Backend 使用 Python 3.11；真实 ZTA35G Runtime 使用隔离的 Python 3.8 环境。不得把
  PyTorch、CUDA、Joblib、scikit-learn 等旧模型依赖安装进 Backend 环境，也不得让两侧共享
  Python 包。
- `SEM/` 是本地只读外部模型包，不进入普通 Git 跟踪。不得修改、移动、删除或借整理/重构
  触碰它；完整性基线是 `docs/acceptance/sem-package-manifest.json`。
- 不把 Tensor、完整 Prompt、Secret、完整 Provider 原始响应、图片 bytes、权重路径、内部
  绝对路径或本地数据写入日志、公共响应或版本库。
- 固定推理参数 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000` 属于 Runtime 合同。
  变更时必须同步合同、实现和测试，不得仅开放为环境变量。
- 不混合不同 ToolRun 的图片、性能结果或解释来源。Backend 不自动重试 Runtime execute；
  显式失败重试创建新 AgentRun、Invocation、ToolRun、attempt 和 seed，重新授权。
  Answer Regeneration 走受限 AgentRun，必须向 DecisionEngine 注入空工具集合，并在执行边界禁止 Tool。
- 外部 LLM、Runtime 和 MinIO 调用不得置于数据库长事务中；通过 version、claim 和短事务 CAS
  取得推进权，已派发动作不得因 lease、轮询或 HTTP 断开而重新派发。
- ToolResult 与 Observation 分段落库时，必须通过确定性一致性门禁后才能继续决策。

更完整的机制说明在 `docs/project-context.md`；以上条目保留在规则层，是因为 Agent 在修改相关
代码前必须直接看到这些边界。

## 产品范围边界

当前版本是单用户、本地运行的模块化单体 MVP，默认应用组合注册
`materials_unit_conversion` Standard Tool 与 `zta35g_sem_virtual_lab` Managed Tool。单一
Tool Registry、LangChain Adapter 和 ExecutorRouter 是显式扩展底座；每个 AgentRun 通过有界循环形成多个有序 Invocation，生产 Catalog 禁止 Side-effect Tool。

Agent Runtime 是唯一执行入口；不得恢复旧 Router、固定 Explanation、旧补参或 Native Tool Calling 独立执行路径。
FinalAnswer 成功持久化后 Run 才能 SUCCEEDED，HTTP 发送结果不改变已提交业务状态。

当前不包含 Redis、后台 Worker、SSE、WebSocket、登录、多用户隔离、真实 SEM 上传、EBSD
输入、ML Training、Planner、多 Agent、动态插件上传或生产部署。增加这些能力属于产品或
架构范围变更；实施前必须获得确认，并同步实现、迁移、测试、README 和项目上下文。

## 修改纪律

- 只修改当前任务需要的文件，不做机会性重构、格式化或依赖升级。
- 公共 API、状态语义、持久化字段、Tool 合同或 Runtime 合同变化时，必须同步所有相关层和
  契约测试，不能留下只在某一层成立的新事实。
- 外部 Provider、真实 Runtime、GPU、Docker 服务和浏览器流程仅在任务明确需要且安全前提
  满足时运行。
- README 只记录项目入口、稳定能力、运行方式和限制；深层架构放
  `docs/project-context.md`；规则放本文件。同一事实不要在三处复制完整版本。
- 不创建动态进度文件、里程碑日志、一次性交接摘要或逐次验收报告。当前状态由 Git、测试、
  当前代码和现场运行检查提供。

## 最小验证

- 修改 `backend/**`：运行 `python -m pytest backend/tests -q`。
- 修改 `frontend/**`：运行 `npm --prefix frontend test -- --run`、
  `npm --prefix frontend run typecheck` 和 `npm --prefix frontend run build`。
- 修改 `scripts/dev/**`：运行对应离线测试；涉及本地栈或范围检查时，分别运行
  `& .\scripts\dev\test-local-dev.ps1` 或 `& .\scripts\dev\test-check-scope.ps1`。
- 修改 `mock-runtime/**`：运行 `python -m pytest mock-runtime/tests -q`。
- 修改 `zta35g-runtime/**`：在 `materialsagent-zta35g` Python 3.8 环境中运行相关 unit、
  contract 或 compatibility 测试；除非任务明确要求，不加载模型或占用 GPU。
- 修改根配置、环境声明、数据库迁移或验收脚本：运行最接近受影响行为的配置、迁移、合同或
  离线验收门禁；没有确定性门禁时，明确报告人工验证依据和未覆盖风险。
- 仅修改文档或规则：运行 `& .\scripts\dev\check-scope.ps1`，用 `-AllowedPath` 精确列出
  本轮获准的每个仓库相对路径。
- 所有改动在声明完成前都必须运行 `git diff --check`，检查最终 diff 和 `git status`，并如实
  报告未运行、跳过或依赖外部环境的验证。

## Code Review Rules

- 优先报告行为语义、状态一致性、数据隔离、幂等与重试、安全边界、数据库迁移和公共合同
  问题；格式化和机械风格问题交给仓库现有工具。
- 每项发现应指出具体文件位置、影响和验证依据；没有发现时仍需说明未覆盖的环境或风险。

## Git 与敏感信息

- 未经项目负责人明确授权，不执行暂存、commit、amend、push、merge、rebase 或创建 PR。
- 禁止使用 `git add .` 和 `git add -A`；获准暂存时只显式列出已批准路径。
- 不提交真实 `.env`、凭据、数据库、对象存储数据、生成输出、模型权重、缓存、日志、会话、
  临时目录、worktree 或机器专用状态。
- `.codex/` 仅允许经明确审阅、无 Secret、无个人绝对路径、无机器状态的稳定项目配置。
- 不使用 `git reset --hard`、`git checkout --` 等破坏性命令覆盖工作区。删除、移动或清理前
  必须确认精确目标、影响和授权范围，并优先采用可恢复方式。
