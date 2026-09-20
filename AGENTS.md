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
| `services/materials_ml/`、`packages/materials_storage/` | 独立 Engine、ML Service/Worker、Prediction/MCP、迁移/验收及共享对象存储引用 |
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
- Materials ML Engine 使用自己的 Python 3.11 环境；不得向 Backend 或真实 Runtime 环境安装
  其依赖。Engine 不依赖 Web、MCP、Agent、数据库、MinIO 或旧研究入口。
- ML 领域写入只允许经过 Materials ML Service 的 Application Layer，内部统一使用中性 scope_id；
  Worker 经 Service API 提交状态和产物，不直接写 ML 表或对象存储。Backend 只持有受控
  ResourceRef，不导入 ML Repository。具体合同见项目上下文。
- `SEM/` 是本地只读外部模型包，不进入普通 Git 跟踪。不得修改、移动、删除或借整理/重构
  触碰它；完整性基线是 `docs/acceptance/sem-package-manifest.json`。
- 不把 Tensor、完整 Prompt、Secret、完整 Provider 原始响应、图片 bytes、权重路径、内部
  绝对路径或本地数据写入日志、公共响应或版本库。
- EBSD 外部研究目录及权重保持只读；EBSD 使用 FP32/eval/no_grad，不修改全局 CUDA/TF32 精度配置。
- 固定推理参数 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000` 属于 Runtime 合同。
  变更时必须同步合同、实现和测试，不得仅开放为环境变量。
- 不混合不同 ToolRun 的图片、性能结果或解释来源。Backend 不自动重试 Runtime execute；
  显式失败重试创建新 AgentRun、Invocation、ToolRun、attempt 和 seed，重新授权。
  Answer Regeneration 走受限 AgentRun，必须向 DecisionEngine 注入空工具集合，并在执行边界禁止 Tool。
- 外部 LLM、Runtime 和 MinIO 调用不得置于数据库长事务中；通过 version、claim 和短事务 CAS
  取得推进权，已派发动作不得因 lease、轮询或 HTTP 断开而重新派发。
- ToolResult 与 Observation 分段落库时，必须通过确定性一致性门禁后才能继续决策。
- Agent Runtime 是业务执行的唯一入口；新 Tool 或 Executor 必须接入统一 Registry 与执行边界，
  生产 Catalog 禁止 Side-effect Tool。不得恢复旧 Router、固定 Explanation、旧补参或独立的
  Native Tool Calling 执行路径。

更完整的机制说明在 `docs/project-context.md`；以上条目保留在规则层，是因为 Agent 在修改相关
代码前必须直接看到这些边界。

## 产品范围边界

本项目按单用户、本地运行的模块化单体 MVP 维护。当前能力、Tool catalog、状态模型以及 ML、MCP、
Prediction、资源与删除机制的现役合同以 `README.md` 和 `docs/project-context.md` 为准，不在本文件复制。

未经项目负责人确认，**MUST NOT** 扩大产品或基础设施范围，例如引入登录/多租户、分布式队列或平台
后台 Worker、Redis、SSE/WebSocket、Planner/多 Agent、动态插件、通用管理后台或生产部署拓扑。
获准的范围变化必须同步实现、迁移、测试、README 和项目上下文，不能只在单层留下新事实。

## 核心工程原则

以下规则中的 **MUST / MUST NOT** 是硬约束，**SHOULD / SHOULD NOT** 是默认选择；偏离默认选择时必须有
当前合同、风险或实证支持，不能只以实现方便或单个测试样例为理由。

### LLM-first：语义推理交给模型，确定性代码守住边界

- 用户自然语言理解、意图与参数语义、上下文指代、歧义识别、工具选择及是否调用工具，**MUST**
  优先由 LLM 基于受控上下文形成结构化 Proposal。Tool catalog 和执行策略可以限制合法选择，
  但确定性代码 **MUST NOT** 暗中替模型完成语义选择。
- **MUST NOT** 用关键词/同义词列表、正则、固定句式、字符串包含判断或针对单一输入的 `if/else`
  代替语义理解。出现大量 `if xxx in text` 或为某个措辞补分支时，**MUST** 先重新判断该能力是否属于 LLM。
  只有明确、稳定、确定性的业务规则才应硬编码。
- 离线 Mock、测试替身或协议夹具可以用确定性匹配模拟有限路径，但 **MUST** 留在模型适配/测试边界，
  **MUST NOT** 定义生产语义、流入真实决策路径，或作为自然语言能力已验证的证据。
- 确定性代码 **MUST** 聚焦类型与数据校验、权限、状态机、安全边界、Tool contract、执行策略、
  幂等、事务、资源限制及 LLM 输出后的结构化验证。Backend 必须校验 Proposal、资源身份、归属、
  类型、Provider、状态和歧义，阻止幻觉 ID 与越权引用。
- LLM **MUST NOT** 重新推断可由 Agent State、ToolResult、schema、权限、状态机或数据库权威字段
  确定性获得的系统事实。确定性代码 **MUST** 读取、校验并按需投影这些事实；模型只在已提供的
  权威事实和约束之上进行语义理解、解释或选择。
- 模型无法确定或明确指向不存在资源时 **MUST** 询问用户或返回 unresolved；Backend **MUST NOT**
  通过最近性、关键词、名称模糊匹配或“唯一候选”自动绑定。临时资源引用只能在当前 ContextFrame 内解码。
- 测试 **SHOULD** 验证语义能力、结构化合同、安全边界和多种等价表达，**SHOULD NOT** 迫使实现匹配
  某个固定措辞。真实 LLM 未独立验收时，**MUST NOT** 宣称自然语言闭环已验证。

### Context Engineering：最小充分上下文

- 每次业务 LLM 调用 **MUST** 明确当前任务、必需事实、权威来源、辅助信息、禁止暴露的信息和输出合同，
  并使用 task-specific Context Profile / Context Policy（或项目中的等价上下文隔离抽象）。统一边界可以复用投影、预算和校验，
  但不同用途 **MUST NOT** 因代码复用而共享一个“大而全”的上下文对象。
- Decision/路由、Tool 参数解析与补参、结果解释、恢复及普通回答等用途 **SHOULD** 有各自的输入白名单和
  安全边界。只有可见范围和合同确实相同时才能复用具体实现；不得绕过项目统一的模型调用与上下文隔离边界建立平行入口。
- **MUST** 遵循 minimum sufficient context / least context：数据库或 Agent State 中的结构化事实可以是系统
  权威状态，但不因此自动成为模型输入。用户可见、模型可见、Runtime 内部、Tool 私有、系统控制、数据库字段、
  调试信息和安全信息 **MUST** 分别定义可见范围。
- 原始 ToolResult/Provider 响应 **MUST** 先经过确定性白名单投影；原始数据库 ID、存储路径、基础设施标识、
  凭据和 Tool 私有实现信息 **MUST NOT** 暴露给模型。当前 ContextFrame 可以签发受控、短期、opaque 的
  model-facing resource handle，但模型不得看到其内部映射，也不得跨 ContextFrame 复用。
- 歧义解析确实需要候选信息时，可以提供经过权限与状态过滤的最小合法候选集合及必要语义摘要；
  **MUST NOT** 暴露全部内部资源状态，也不得仅为丰富 prompt 对全部候选执行远端查询或状态扇出。
- 新增或修改模型调用时 **MUST** 检查 context leakage、无关历史污染、Tool 内部信息泄露、系统状态泄露、
  重复与过时事实；“数据已经存在”不是将其加入 prompt 的理由。

### 在正确层解决通用问题

- 实现前 **MUST** 判断问题属于 LLM semantic reasoning、Context Policy/Profile、Agent Runtime、Tool contract、
  deterministic backend logic、frontend interaction 还是 persistence，并在真正拥有该责任的层修复。
- **MUST NOT** 因为当前正在修改某个文件就把问题塞入该层。例如，用户理解错误不等于要加后端字符串规则，
  模型判断错误不等于要增加更多上下文，Tool 输入错误也不能用 prompt 绕过 schema validation。
- 修复缺陷或增加能力时 **SHOULD** 先回答“系统缺少什么通用能力”，再处理当前 case。只适用于当前输入、
  字段名、句式或工具的补丁 **SHOULD NOT** 合入；确属业务特例时，必须在正确合同层明确建模并覆盖测试。

### 前端是 Agent 产品，不是实体管理后台

- 对话 **MUST** 是核心交互入口，前端 **MUST** 围绕用户任务和 Agent 协作设计，而不是围绕数据库实体、
  Task、Invocation、ToolRun、Action 或 Observation 建页面。后端新增实体本身 **MUST NOT** 自动推导出表格、
  侧栏或管理面板。
- Chat、Composer、补参、确认、Tool 调用、长任务、结果、失败恢复与重试 **SHOULD** 采用成熟 AI 产品的
  Agent UX 原则：状态变化融入对话，结果自然成为消息或卡片，用户能理解当前在做什么，但无需理解内部实现。
  参考的是交互原则，不是复制 ChatGPT、Claude 的视觉样式。
- 长任务 **MUST** 提供清晰且面向用户的 pending/running/success/failure 反馈；普通用户界面的错误 **MUST**
  转译为可行动的表达，**MUST NOT** 直接暴露后端异常、内部状态码、原始 JSON 或实现细节。受控的本地开发诊断除外。
- 补参、确认、恢复和重试 **SHOULD** 尽量在当前上下文内完成，**SHOULD NOT** 引入不必要的弹窗、页面跳转
  或阻塞式确认；安全、授权或不可逆操作明确要求确认时除外。技术复杂度应由产品体验隐藏。

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

## 风险驱动验证

- 验证范围 **MUST** 由受影响行为、合同边界和失败风险决定，**MUST NOT** 仅按修改目录机械选择。
  开发阶段默认先运行直接相关的 targeted tests；只有证据不足时才逐步扩大范围。
- 跨模块改动、公共 API/Tool/Runtime 合同、Agent Runtime、持久化或 migration、核心执行链路、
  大范围重构，以及里程碑或最终验收属于高风险场景，**MUST** 运行相关完整回归或 acceptance。
- Backend 与 Mock Runtime 的局部改动优先运行直接相关的 pytest 文件或测试节点；高风险 Backend 改动再运行
  `python -m pytest backend/tests -q`。真实 Runtime 在隔离的 Python 3.8 环境运行相关 unit、contract 或
  compatibility 测试；除非任务明确要求，不加载模型或占用 GPU。
- Frontend 局部改动优先运行直接相关的 Vitest 文件；涉及共享 TypeScript 类型、API 或组件合同再运行
  `npm --prefix frontend run typecheck`，涉及构建配置、依赖或打包行为再运行 build。高风险或最终验收时运行
  `npm --prefix frontend test -- --run`、`npm --prefix frontend run typecheck` 和
  `npm --prefix frontend run build`。
- `scripts/dev/**` 运行对应离线测试；只有本地栈或范围检查行为受影响时，才分别运行
  `& .\scripts\dev\test-local-dev.ps1` 或 `& .\scripts\dev\test-check-scope.ps1`。
- Materials ML Engine、存储引用、Service 或 Worker 改动必须在独立 ML Python 3.11 环境验证相关范围。
  涉及跨服务闭环或最终验收时运行 `services/materials_ml/scripts/acceptance.ps1`，使用独立临时 PostgreSQL/MinIO
  资源且不得清空共享卷；没有真实 Prediction/MCP Client/故障恢复证据不得宣称相应闭环已验收。
- 平台 MCP 执行、资源接入/删除 fence、聊天附件/结果观察等核心合同发生变化或进入对应最终验收时，
  分别运行 `services/materials_ml/scripts/acceptance-p4.ps1`、`acceptance-p5.ps1`、`acceptance-p7.ps1`
  及其要求的独立 Backend Python；真实 LLM 与浏览器验证保持独立 opt-in，不得由协议单测、确定性模型
  或 API 测试推断其已通过。
- 根配置、环境声明、migration 或验收脚本变更运行最接近受影响行为的配置、迁移、合同或离线门禁；
  没有确定性门禁时，明确报告人工验证依据和未覆盖风险。
- 仅修改文档或规则：运行 `& .\scripts\dev\check-scope.ps1 -Label <本轮标签>`，用 `-AllowedPath`
  精确列出本轮获准的每个仓库相对路径。
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
