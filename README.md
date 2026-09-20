# Materials Agent MVP

这是一个面向材料研究的本地材料智能体 MVP。用户通过自然语言创建对话，可调用默认应用组合中
的材料单位换算 Standard Tool，或通过受严格治理的 `zta35g_sem_virtual_lab` Managed Tool，
根据 ZTA35G 热处理工艺参数生成 SEM 图像及力学性能；也可上传单张 EBSD 图片，通过
`ebsd_yield_strength_predictor` Managed Tool 预测 Inconel 625 屈服强度，保留输入来源和最终回答。

## Quick Context

| 项目维度 | 当前事实 |
|---|---|
| 项目类型 | 单用户、本地运行的材料研究智能体；Backend 是模块化单体，模型执行在隔离 Runtime 中 |
| 当前阶段 | 可运行 MVP，不是生产部署方案 |
| 主要能力 | 自然语言目标 → 有界 Agent Loop → 多工具连续编排 → 持久化最终回答 |
| 默认 Tool | `materials_unit_conversion`（Standard）、`zta35g_sem_virtual_lab` 与 `ebsd_yield_strength_predictor`（Managed） |
| 扩展底座 | 单一 Tool Registry、统一 ToolDefinition、LangChain Tool Adapter 与 ExecutorRouter |
| 数据存储 | PostgreSQL/MinIO 保存平台事实与图片；可选 ML Service 使用独立数据库、角色和 bucket |
| 平台明确不包含 | 登录、多用户、Redis、平台后台 Worker、SSE/WebSocket、上传真实 SEM、多 Agent、动态插件和生产部署 |

仓库当前代码、配置、迁移、测试和实际运行行为是事实来源。`AGENTS.md` 规定 Coding Agent 的
工作规则；`docs/project-context.md` 记录长期架构、Tool 机制和状态模型。旧设计、计划和验收
记录只在 Git 历史中追溯，不作为当前合同。

## 当前能力

- 有界 `Decision → Action → Observation → Decision` 循环；每个 AgentRun 默认最多 12 步、4 次工具执行、3600 秒活跃时间和 32000 Token。
- 三类结构化动作：`CallTool`、`AskUser`、`Finish`；支持预测性能后继续单位换算。
- 区分目标澄清和工具补参；用户补充信息后按等待版本恢复原 Run，保留累计预算。
- Managed Tool 继续以 Task、InputRevision、ToolRun、ToolResult、Asset 保存独立来源。
- FinalAnswer、assistant 消息和成功状态原子提交；网络断连不撤销已保存答案。
- 失败 Tool Retry 与受限 Answer Regeneration 创建新 Run；回答再生成禁止执行工具。
- 确认绑定具体 Invocation 和参数版本；生产目录禁止 Side-effect Tool，开发测试工具仅用于验证确认机制。
- 聊天界面展示消息、语义进度、最终回答与附件/结果卡片；开发诊断单独受开关控制。
- ML 聊天可根据字段名推断单位，并明确标注推断依据；不会修改数据集登记信息。涉及数值换算等操作时须先确认未知单位。
- Conversation 删除原子移除业务聚合，并通过持久化 cleanup 记录清理本项目生成对象及上传的 EBSD 图片；运行中的聚合拒绝删除。
- Mock 或 DeepSeek/Qwen Provider；AgentRun 由请求宿主同步推进，不提供平台后台 Worker、队列或断点接管。
  可选 ML Service 的训练 Worker 是独立领域进程，不接管 AgentRun。

### EBSD 单图预测

聊天输入区使用“＋”上传文件并描述需求。EBSD 图片和 CSV 共用入口，每条消息最多一个附件；缺图时可补图恢复。
服务端实际解码，只接受单帧 RGB PNG/JPEG、正方形、边长 128–4096 像素、最大 10 MiB，
不自动裁剪或改变配色。当前只支持 Inconel 625，输出屈服强度 MPa；数值原精度持久化，界面显示
两位小数。图像编码要求与适用数据分布尚未核实，不提供置信区间，也不将输入图片标为生成产物。
LLM 只接收文本和语义图片引用，不接收图片内容或数据库 ID。移除草稿引用不删除存储图片，删除所属对话会清理它。

## 架构概览

```text
Vue 3 + Vite frontend (127.0.0.1:3000)
                |
                v
FastAPI modular monolith (127.0.0.1:8000, Python 3.11)
       |                         |
       v                         v
PostgreSQL + MinIO       local Tool client adapter
                                  |
                                  v
             Mock or real ZTA35G Runtime (127.0.0.1:8100)
                    real Runtime: Python 3.8 + GPU model
```

Backend 负责公共 API、Agent Loop、Tool Registry、Tool catalog、状态转换和持久化。Runtime
提供 SEM 与 EBSD 各自的 health/ready 和单次受控 execute 接口，共用进程、端口、GPU 和执行锁；它是旧模型的兼容与依赖
隔离边界，不表示平台采用通用微服务架构。Backend 与真实 Runtime 不共享 Python 包。

Tool 路由、绑定、`schema_hash`、Task/ToolRun 状态机、事务与重试语义见
[`docs/project-context.md`](docs/project-context.md)。

## 仓库目录

| 路径 | 职责 |
|---|---|
| `backend/` | FastAPI 模块化单体、Alembic 迁移和后端测试 |
| `frontend/` | Vue 3 + Vite + TypeScript 前端和测试 |
| `mock-runtime/` | Python 3.11 的合同一致 Mock Runtime |
| `zta35g-runtime/` | Python 3.8 的真实 ZTA35G Runtime 适配层和隔离测试 |
| `environments/` | Backend 与真实 Runtime 的 Conda 环境声明 |
| `services/materials_ml/` | 独立 Materials ML Engine、Service、Worker、Prediction、MCP 与验收 |
| `packages/materials_storage/` | stdlib-only 共享对象存储引用值对象 |
| `scripts/dev/` | 本地栈、改动范围和 SEM 完整性脚本 |
| `scripts/acceptance/` | 可重复运行的验收与离线检查入口 |
| `docs/project-context.md` | 长期架构、数据流、状态与设计边界 |
| `docs/acceptance/sem-package-manifest.json` | `SEM/` 文件大小和 SHA-256 机器基线 |
| `SEM/` | 本机只读外部研究模型包；不进入普通 Git 跟踪 |
| `tmp/` | 被忽略的本地状态、日志和输出；不是项目事实来源 |

更细的后端分层与修改导航见 `AGENTS.md`。

## 环境要求

- Windows PowerShell 5.1 或兼容 PowerShell；
- Docker Desktop 和 Docker Compose；
- `materialsagent-backend` Conda 环境，Python 3.11；
- Node.js `24.14.x` 与 npm `11.9.x`；
- 启用 Materials ML 时需要独立的 Python 3.11 环境 `services/materials_ml/.venv`；
- 使用真实模型时需要 `materialsagent-zta35g` Conda 环境（Python 3.8.20）、兼容 GPU 和只读
  `SEM/ZTA35G_lab` 模型包。

本地启动入口假定所选模式需要的 Conda 环境、Frontend `node_modules` 和 Compose 中固定
摘要的 PostgreSQL/MinIO 镜像已准备完成。入口不会创建或更新 Conda 环境、安装 Python 或
Node.js 依赖，也不会拉取镜像。

依赖版本的机器权威分别是：

- Backend：`backend/pyproject.toml` 与 `environments/materialsagent-backend.yml`；
- Frontend：`frontend/package.json` 与 `frontend/package-lock.json`；
- 真实 Runtime：`environments/materialsagent-zta35g.yml` 和
  `zta35g-runtime/requirements-win-py38.lock.txt`。

旧模型的 PyTorch、Torchvision、CUDA、NumPy、Joblib 和 scikit-learn 依赖只能存在于真实
Runtime 环境，不得安装到 Backend 环境。

## 本地配置

把 `.env.example` 复制为被 Git 忽略的根 `.env`，并为本机填写数据库和 MinIO 配置。
使用 Provider 模式时，根据 `backend/config/llm.toml` 中三个角色实际选择的模型设置
`DEEPSEEK_API_KEY` 和/或 `DASHSCOPE_API_KEY`。LLM 配置在 `.env` 中只保存 Secret；Provider、
模型名、每个具体模型的上下文窗口、temperature、top-p、top-k、Reasoning 和各角色 token
预算都在该 TOML 中配置，修改后重启
Backend 生效。不要提交真实 `.env`，也不要在终端、日志或问题报告中打印 Secret。

`backend/config/llm.toml` 包含受控模型目录、全局默认模型、模型能力声明，以及
`agent_decision`、`tool_arg_resolution`、`final_answer` 三个角色的独立覆盖。
DeepSeek 固定走官方 endpoint；Qwen 固定走阿里云百炼国内 OpenAI-compatible endpoint。
未知 Provider、模型引用、角色或参数会使 Backend 启动失败，不会自动切换或重试其他 Provider。

固定推理参数不是配置项：

```text
num_samples=1
guide_scale=2.0
timesteps=1000
```

`scripts/dev/local-dev.ps1` 每次启动会在内存中生成独立 Runtime token，只注入 Backend 与
Runtime 子进程，不写入状态文件。入口根据根 `.env`（进程环境可覆盖）中的三个 ML 功能开关
确定托管进程集合；ML 配置加载器核验两侧凭据后，仅在内存中把 Worker token 注入 Worker。
凭据不会进入命令参数、状态文件或启动输出，入口也不会修改配置文件。

## 启动

默认组合是 Mock Runtime + Mock LLM：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/dev/local-dev.ps1 `
  -Action Start
```

真实 Runtime + 配置文件选定的 Provider：

```powershell
.\scripts\dev\local-dev.ps1 Start -Runtime Real -Llm Provider
```

启用 EBSD 时通过 `-EbsdModelRoot <外部研究目录>` 或环境变量 `EBSD_MODEL_ROOT` 指定含
`model/save/CNN_1.pt` 的只读目录。加载前核对适配层内固定的 SHA-256；不复制权重到仓库。
未配置或加载失败仅使 EBSD 不可用，SEM 可继续运行。EBSD 使用 FP32、eval、no_grad，
保持原有 CUDA/TF32 设置；两工具忙时返回 BUSY，不自动重试。

也可以独立组合 `-Runtime Mock|Real` 与 `-Llm Mock|Provider`。真实 Runtime 首次加载较慢时，
可把 ready 等待上限从默认 300 秒调整到 10–900 秒：

```powershell
.\scripts\dev\local-dev.ps1 Start `
  -Runtime Real `
  -Llm Mock `
  -ReadyTimeoutSeconds 600
```

入口依次准备 PostgreSQL/MinIO、数据库迁移和 bucket，再等待 Runtime、Backend 与 Frontend
ready。根 `.env` 中任一 ML 功能开关启用时，还会使用 `services/materials_ml/.venv` 和独立
`services/materials_ml/.env` 执行 ML 迁移，启动 ML Service 与 Worker，并纳入状态、日志、健康检查和停止流程。
不会自动 provision 或重建 ML 数据库、角色和 bucket。固定回环端口为：

| 服务 | 地址 |
|---|---|
| Frontend | `http://127.0.0.1:3000` |
| Backend | `http://127.0.0.1:8000` |
| Runtime | `http://127.0.0.1:8100` |
| ML Service（启用 ML 时） | `http://127.0.0.1:8200` |
| ML Worker（启用 ML 时） | 无监听端口 |
| PostgreSQL | `127.0.0.1:5432` |
| MinIO API | `http://127.0.0.1:9000` |
| MinIO Console | `http://127.0.0.1:9001` |

任一固定端口已被未知进程占用时，入口会拒绝启动，不会接管或停止占用者。Compose 使用固定
镜像摘要和 `pull_policy: never`，不会自行拉取或替换镜像。

## 停止

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/dev/local-dev.ps1 `
  -Action Stop
```

停止入口只终止能够通过 PID、启动时间、可执行文件和命令标记证明属于本轮的进程。它只停止
本轮拥有的 Compose 服务，不执行 `down -v`，不会删除数据库 volume、bucket、对象或日志。
不要绕过 ownership 检查按进程名或端口批量终止进程。

## 健康检查与 API

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000/
docker compose -f docker-compose.yml ps
```

主要公共 API 位于 `/api/v1`，包括 health、conversations、attachments、agent-runs（详情、分页 trace、确认和重试）、
tools、tool-results、assets，以及启用后可用的消息 Artifact/结果观察和 ML 资源代理。具体请求/响应结构以
FastAPI 路由、Pydantic schema 和合同测试为准，
不要在文档中复制一份容易漂移的完整协议。

## 测试与检查

Backend 全量测试包含真实 PostgreSQL 和 MinIO 集成测试，需先按上文准备 `.env` 并启动本地依赖。
数据库测试使用随机命名的临时测试库；对象存储测试使用唯一对象键并在测试后清理。它们不会调用
真实 LLM Provider 或 GPU，但不能在 PostgreSQL/MinIO 停止时当作纯单元测试运行。

激活 `materialsagent-backend` 环境后运行 Backend 回归：

```powershell
python -m pytest backend/tests -q
```

Frontend：

```powershell
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

Mock Runtime 使用 Backend 环境：

```powershell
python -m pytest mock-runtime/tests -q
```

真实 Runtime 的安全 unit 与 HTTP contract 测试必须在 `materialsagent-zta35g` 环境中运行：

```powershell
python -m pytest zta35g-runtime/tests/unit zta35g-runtime/tests/contract -q
```

compatibility 测试按改动选择 `zta35g-runtime/tests/compatibility/` 中的对应文件。模型加载和
最小推理测试会使用只读 `SEM/` 包，并可能占用 GPU；只有任务明确要求时才运行。

本地入口和范围检查的离线测试：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/dev/test-local-dev.ps1
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/dev/test-check-scope.ps1
```

检查只读 SEM 包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/dev/check-sem-integrity.ps1
```

仅允许本次三个文档路径时，精确范围检查示例：

```powershell
& .\scripts\dev\check-scope.ps1 `
  -Label DOCS `
  -AllowedPath @('AGENTS.md', 'README.md', 'docs/project-context.md')
```

范围检查只接受仓库相对文件路径，不接受通配符、目录级授权或父目录跳转。任何完成声明都应
基于本轮新执行的相关门禁、`git diff --check` 和最终 Git 状态。

## 手动验收 Agent Loop

验收自然语言决策时，使用 `-Runtime Mock -Llm Provider` 启动，再打开 Frontend。这会调用配置的
真实 LLM，但工具返回 Mock 结果；只能验证编排流程，不能验证材料模型的科学有效性。

每个独立场景新建对话，展开卡片的“查看执行过程”，同时核对状态、动作顺序和实际工具调用次数。
补参前可能直接 AskUser，也可能先 CallTool 校验再 AskUser；缺参时实际工具调用次数都应为零。

| 场景 | 操作 | 应观察到的结果 |
|---|---|---|
| 无工具回答 | 发送“你好” | Finish 后显示最终回答，0 次工具调用 |
| 单工具 | 发送“650 MPa 转 GPa” | CallTool 后再次决策、Finish；结果为 0.65 GPa，1 次工具调用 |
| 缺参暂停 | 发送“对 ZTA35G 进行虚拟实验：固溶温度 1000℃，固溶时间 2 小时。预测力学性能。” | 等待补充时效温度、时效时间；不重复询问可靠已知条件，不执行工具 |
| 原 Run 恢复 | 在上一张卡片点击“补充信息”，输入“时效温度 730℃，时效时间 2 小时” | 原 Run 继续，保留累计步骤和预算；工具结果后再次决策并结束，1 次工具调用 |
| 连续编排 | 新对话发送“对 ZTA35G 进行虚拟实验：固溶温度 1000℃，固溶时间 2 小时，时效温度 730℃，时效时间 2 小时。先预测力学性能，再调用单位换算工具，把预测的屈服强度从 MPa 转为 GPa。” | ZTA35G → 单位换算 → Finish，2 次工具调用；换算输入来自本次预测结果 |
| 回答再生成 | 对已有成功工具结果的终态 Run 点击“重新生成回答” | 新 Run 引用原成功结果，0 次工具调用；原 Run 和工具产物不变 |
| EBSD 单图预测 | 上传合规图片后发送“请预测这张 Inconel 625 EBSD 图片对应的屈服强度” | 1 次 EBSD 调用；展示屈服强度、MPa、输入图片和实验性限制，不显示延伸率或热处理条件 |
| EBSD 缺图恢复 | 新对话不上传图片，发送“请预测 Inconel 625 的 EBSD 图片对应的屈服强度”，等待后通过“补充信息”上传图片并提交 | 缺图时不推理；补图后原 Run 继续，刷新仍能追溯输入与结果 |

EBSD 的 Mock Runtime 返回固定模拟值并标注 Mock；验证真实 CNN 时使用 `-Runtime Real -Llm Provider`
并配置 `-EbsdModelRoot`。不要用模拟值判断模型准确性；真实 SEM 与 EBSD 共用执行锁，BUSY 时显式重试。

在等待补参和完成后分别刷新页面，应恢复服务端状态。网络结果不确定时使用“检查原提交”，不要
新建同一个目标。测试中文输入法候选确认不会发送、Shift+Enter 换行，以及窄屏下补参和结果可操作。
已经“已结束”的 Run 不能因修复代码而恢复；排除错误后创建新目标，或使用符合资格的显式重试。

默认 Mock LLM 是固定规则：支持简单单位换算、ZTA35G 缺参询问、EBSD 资产引用调用和 JSON 参数增量，不支持任意中文
提取或上述多工具自然语言编排。其输出只验证协议和界面。确认、拒绝、过期及可重试失败用受控测试
工具验证，默认注册的三个工具不会触发确认。并发 CAS、事务、超时和 Token 预算边界以自动化测试为准。

## 安全与数据边界

- PostgreSQL 保存结构化业务事实，MinIO 保存图片；日志和公共响应不保存图片 bytes。
- 不记录完整 Prompt、完整 Provider 原始响应、Tensor、Secret、权重路径或内部绝对路径。
- AgentModelCall 保存 Usage、角色、输出额度和 Prompt digest，不复制完整 Prompt；ToolResult
  只通过 Tool 注册的安全投影或 metadata-only 投影进入上下文。
- `.env`、数据库、MinIO 数据、生成输出、模型权重、缓存、日志和 `tmp/` 都不得提交。
- `SEM/` 始终只读；完整性脚本只枚举文件并校验大小和 SHA-256，不导入模型代码。
- 外部 LLM、Runtime 与 MinIO 调用不得放在数据库长事务中。
- 不混合不同 ToolRun 的资产与结果；显式重试创建新的 ToolRun 和 seed。
- Conversation 删除后的 MinIO cleanup 会在响应后、应用启动和后续删除时按上限重试；也可运行
  `python -m materialsagent.maintenance.cleanup_conversation_objects --limit 100` 进行一次维护重试。

## 独立 Materials ML Service

`services/materials_ml/` 提供独立 Python 3.11 的 LR/RF 表格分析、训练、评估、模型包保存/加载与预测。
独立 ML Service 提供 Dataset/TrainingRun/Model/Prediction Resource API、专属 PostgreSQL/MinIO 存储、Windows Worker，
以及默认关闭的四 Tool MCP 入口；Prediction 在请求监管的独立子进程执行。
可上传 CSV、提交训练、查询预测并下载结果。平台另有默认关闭的 MCP Executor 与四个精确注册 Tool，
仅供 local/test 使用。独立资源入口提供 Dataset 上传代理、ResourceRef、查询下载和对话删除协调；
可单独启用可信资源上下文与自然语言引用；聊天页通过统一上传与消息附件/结果查看器访问资源。
安装、独立启动、调用示例和验收入口见 [ML 使用说明](services/materials_ml/README.md)；领域写入边界与
已实现合同和后续边界见 [项目上下文](docs/project-context.md#materials-ml-领域边界)。不要将 ML 依赖安装到 Backend 环境。

启用平台 MCP 底座前，在 Backend Python 3.11 环境安装 `python -m pip install -e "./backend[mcp]"`，
执行既有 Backend Alembic `upgrade head`（当前 head 包含 0021），并启动启用 MCP 的独立 ML Service/Worker。
根 `.env` 设置 `ENABLE_DEV_MATERIALS_ML_TOOLS=true`，配置 `MATERIALS_ML_MCP_URL`、
`MATERIALS_ML_BINDING_VERSION` 和不同的 `MATERIALS_ML_MCP_TOKEN` / `MATERIALS_ML_RESOURCE_TOKEN`。
后两者分别对应 Service 的 MCP / Resource token；Backend 不配置 ML Worker 凭据。
预测可能耗时 30 秒；需要完整同步等待时显式配置 `AGENT_STANDARD_TIMEOUT_SECONDS=60`，
否则服从原有较短预算并进入受控取消/回执核查。production 不能启用此开关。
`backend/config/llm.toml` 中决策角色使用 50000 上下文上限，覆盖完整工具目录与
当前附件摘要在离线词表不可用、采用 UTF-8 字节保守估算时的首轮请求。补参和最终解释仍为 8K。
请求仍受模型窗口、1024 安全余量、输出预留和 AgentRun 总 Token 预算（默认 32000）约束；裁剪后仍超限时
拒绝调用并返回语义化说明，不自动扩大预算。

训练成功回执只表示 TrainingRun 已提交；同步预测只正常返回终态。结果不确定时 Agent 立即停止，
可通过 Invocation 的 `/receipt` 与显式 `/reconcile` 接口核查原操作，不能普通重试或假定远端未创建资源。
资源接入单独设置 `ENABLE_MATERIALS_ML_RESOURCES=true`，复用 Resource token 和可信服务地址，不要求开启 MCP Tool 注册。
ML Service 先执行自身迁移至 0003。平台资源 API 位于 `/api/v1/conversations/{id}/ml`：
上传 Dataset、显式登记资源、查询权威状态并下载固定文件；训练/预测创建仍走原 Tool 治理。
ResourceRef 仅证明身份曾核实，不能作为可用性缓存。活动 ML 工作或未确认操作导致删除返回 BUSY；
远端关闭结果未知时保留对话与持久化 fence，禁止新工作，使用原删除 operation 查询/reconcile。
仅在 ML scope 已关闭并接管清理后删除平台聚合；不会由删除动作自动取消训练或预测。

资源上下文单独设置 `ENABLE_MATERIALS_ML_RESOURCE_CONTEXT=true`，要求上述 MCP 与资源入口均已启用。
模型在正常 AgentDecision 中根据受控资源摘要输出临时 `resource_ref` 或 `unresolved`，Backend 再核验归属、类型和状态。
即使只有一个候选，Backend 也不绕过语义提案强行绑定；无法确定时通过通用补参流程询问用户。
用户无需复制 Dataset/TrainingRun/Model ID。训练与预测仍在聊天中发起并通过现有确认边界执行。

上传 UTF-8 CSV 后直接描述需求，例如“用这个数据预测屈服强度”。聊天不提供资源管理、算法配置或训练页面。
消息上的“查看详情”可查看数据概况、评估指标、预测预览和图片；查看不选择工具输入，也不触发 Agent。
Dataset 原 CSV 和语义化 Prediction CSV 可下载，模型包 ZIP 暂不提供。
长任务只显示临时进度，并在终态追加一条去重结果消息；刷新、多标签页和回包丢失不会改写原回答。
进入对话时独立核查已确认关联任务，每轮最多 4 项、每 5 秒一次、单窗口最多 5 分钟，之后可手动继续核查。
Unknown 仅核查原操作，不自动重派发。上传断连使用原键核查，没有查到记录不能自动重复上传。

升级先停止当前应用写入。旧业务数据不兼容读取时，必须先运行预检并完成获准的聚合清理：

```powershell
python scripts/dev/upgrade-chat-data.py
# 核对输出的完整对话清单后，逐个显式指定；BUSY/UNKNOWN/归属不明会停止。
python scripts/dev/upgrade-chat-data.py --apply --conversation <对话ID>
```

使用独立 Backend Python 3.11；迁移历史保留，清理不触碰共享卷、模型包或凭据。
预检集全部安全收敛后再执行 Backend Alembic `upgrade head`；新库或已兼容数据库可直接迁移。

P7 自动验收入口为 `services/materials_ml/scripts/acceptance-p7.ps1 -BackendPython <Backend Python 路径>`，
`-RealLLM` 单独开启真实语言验收。真实浏览器验收另设 opt-in：构建前端后，设置
`ML_INTEGRATION=1`、`P4_BACKEND_PYTHON` 和 `P7_BROWSER_ACCEPTANCE=1`，在独立 ML 环境运行
`pytest services/materials_ml/tests/service/test_platform_resource_browser.py -q -s`。
该入口监管临时服务与合成数据，默认使用脚本模型检查界面和真实工具链；只有额外设置
`P7_BROWSER_REAL_LLM=1` 才使用真实 Provider。必须实际完成浏览器检查后记录检查结果，
脚本模型的界面验收不能替代真实语言验收。

P6 验收入口为 `services/materials_ml/scripts/acceptance-p6.ps1 -BackendPython <Backend Python 路径>`。
资源引用由 LLM 解析并提出结构化绑定，Backend 核验身份、权限、状态及歧义；不要求用户使用固定口令。
同对话唯一 Dataset 可以用自然指代，多候选依据上下文消歧，无法确定时再询问。
基础套件使用确定性模型与真实基础设施；追加 `-RealLLM` 才运行独立的真实语言验收，使用现有 Provider 配置、
合成数据和有限元数据。未运行或未通过真实语言验收不能宣称完整 P6 通过。

完整隔离验收入口为 `services/materials_ml/scripts/acceptance-p5.ps1 -BackendPython <Backend Python 路径>`；
入口使用临时数据库、bucket 和合成数据，不加载 SEM 模型。

## 当前限制

当前聊天平台不提供登录、多用户隔离、Redis、平台后台 Worker、SSE、WebSocket、任意格式附件或多文件消息、
独立 ML 管理页、Planner、多 Agent、动态插件上传或生产部署能力。ZTA35G Tool 只适用于 ZTA35G
钛合金；不支持上传真实 SEM 后预测；只请求力学性能时仍会生成中间 SEM。对话记忆不跨
Conversation，也暂不提供滚动摘要、记忆开关或管理界面。

## 常见问题

- `LOCAL_DEV_CONFIGURATION_MISSING name=DEEPSEEK_API_KEY`：在根 `.env` 配置 Key，不要把值
  贴入命令或日志。
- `LOCAL_DEV_CONFIGURATION_MISSING name=DASHSCOPE_API_KEY`：当前角色选择了 Qwen；在根
  `.env` 配置百炼 Key，不要把值贴入命令或日志。
- `LOCAL_DEV_PORT_CONFLICT`：确认固定端口占用者；脚本不会替你终止未知服务。
- `LOCAL_DEV_READY_TIMEOUT`：查看本轮 `tmp/local-dev/<run_id>/` 日志；真实 Runtime 重点检查
  环境、只读模型根和 GPU。
- `LOCAL_DEV_STATE_REQUIRES_STOP`：先使用正式 Stop 入口清理已记录资源，不要直接删除状态
  文件后宽泛杀进程。
- `LOCAL_DEV_PROCESS_OWNERSHIP_NOT_VERIFIED` 或
  `LOCAL_DEV_DOCKER_OWNERSHIP_NOT_VERIFIED`：脚本无法证明对象属于本轮；核对状态和日志后
  再处理。

## 文档与历史

- `AGENTS.md`：Coding Agent 的行为规则、禁止事项和验证义务；
- `README.md`：项目入口、能力、运行、测试和当前限制；
- `docs/project-context.md`：长期架构、数据流、Tool 机制、状态与设计边界；
- `DESIGN.md`：界面组件职责、交互和视觉约束。

不要新增动态进度、里程碑日志、一次性交接摘要或逐次验收报告。当前状态通过 Git、测试和
现场运行检查确认。需要查看已经删除的旧文档时：

```powershell
git log --all -- <旧文档路径>
git show <commit>:<旧文档路径>
```

Git 历史用于追溯“过去为何这样做”，当前行为仍以现有代码、配置、迁移、测试和运行结果为准。

## Agent Runtime 验收

Backend 迁移必须保留历史并升级到当前 head；不兼容业务数据按上文预检与清理流程处理，迁移本身不
自动清空共享数据。旧 Router、固定解释和补参执行入口保持退役，不得作为回退路径恢复。

离线统一入口：`scripts/acceptance/run-agent-acceptance.ps1 -BackendPython <Python 3.11 路径>`。
该入口运行 Backend、Mock Runtime、前端和脚本门禁，需要本地 PostgreSQL/MinIO；不发起真实 Provider
或 GPU 推理，也不负责启动服务、安装依赖或拉取镜像。
浏览器验证与真实 Provider/GPU 实测分别记录，测试通过不等于服务正在运行。

默认 ZTA35G 单次超时为 1200 秒（配置上限 86400 秒），实际调用仍受 Run 剩余活跃时间限制。
本地 Vite 代理允许 3660 秒请求；没有额外生产反向代理。超时表示停止等待，不表示远端 GPU 已取消。

### EBSD Runtime 独立验收

在真实 Runtime 的 Python 3.8 环境中，设置 `EBSD_MODEL_ROOT` 和 `EBSD_REAL_MODEL_TEST=1`，
运行 `python -m pytest zta35g-runtime/tests/compatibility/test_ebsd_http_gpu.py -q -s`。
测试独立启动临时 HTTP 服务，不依赖 Backend、Provider、数据库或 MinIO。额外设置
`EBSD_SEM_HTTP_TEST=1` 会在同一进程执行两轮完整 SEM → EBSD，并核对状态、BUSY、TF32 和权重。
该选项包含完整 1000 步 SEM 推理，会长时间占用 GPU；不要与平台的 SEM 推理同时运行。
普通 Runtime 测试默认跳过真实权重/GPU 验收。
