# Materials Agent MVP

这是一个面向材料研究的本地材料智能体 MVP。用户通过自然语言创建对话，可调用默认应用组合中
的材料单位换算 Standard Tool，或通过受严格治理的 `zta35g_sem_virtual_lab` Managed Tool，
根据 ZTA35G 热处理工艺参数生成 SEM 图像，并按请求获得力学性能结果和可追溯的最终回答。

## Quick Context

| 项目维度 | 当前事实 |
|---|---|
| 项目类型 | 单用户、本地运行的材料研究智能体；Backend 是模块化单体，模型执行在隔离 Runtime 中 |
| 当前阶段 | 可运行 MVP，不是生产部署方案 |
| 主要能力 | 自然语言目标 → 有界 Agent Loop → 多工具连续编排 → 持久化最终回答 |
| 当前 Tool | `materials_unit_conversion`（Standard）与 `zta35g_sem_virtual_lab`（Managed） |
| 扩展底座 | 单一 Tool Registry、统一 ToolDefinition、LangChain Tool Adapter 与 ExecutorRouter |
| 数据存储 | PostgreSQL 保存结构化事实，MinIO 保存生成图片 |
| 明确不包含 | 登录、多用户、Redis、后台 Worker、SSE/WebSocket、上传真实 SEM、多 Agent、动态插件和生产部署 |

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
- 聊天界面按 Run 展示工具结果、询问、最终回答和执行记录；刷新和轮询读取服务端状态。
- Conversation 删除原子移除业务聚合，并通过持久化 cleanup 记录清理本项目生成对象；运行中的聚合拒绝删除。
- Mock 或 DeepSeek/Qwen Provider；请求内执行，不提供 Worker、队列或断点接管。

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
只提供 health/ready（含受支持 Tool 元数据）与一次受控 execute；它是旧模型的兼容与依赖
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
Runtime 子进程，不写入状态文件。入口不自行解析、复制、打印或修改根 `.env`；Backend
配置加载器和 Docker Compose 会按需直接读取该文件。

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

也可以独立组合 `-Runtime Mock|Real` 与 `-Llm Mock|Provider`。真实 Runtime 首次加载较慢时，
可把 ready 等待上限从默认 300 秒调整到 10–900 秒：

```powershell
.\scripts\dev\local-dev.ps1 Start `
  -Runtime Real `
  -Llm Mock `
  -ReadyTimeoutSeconds 600
```

入口依次准备 PostgreSQL/MinIO、数据库迁移和 bucket，再等待 Runtime、Backend 与 Frontend
ready。固定回环端口为：

| 服务 | 地址 |
|---|---|
| Frontend | `http://127.0.0.1:3000` |
| Backend | `http://127.0.0.1:8000` |
| Runtime | `http://127.0.0.1:8100` |
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

主要公共 API 位于 `/api/v1`，包括 health、conversations、agent-runs（详情、分页 trace、确认和重试）、tools、
tool-results 和 assets。具体请求/响应结构以 FastAPI 路由、Pydantic schema 和合同测试为准，
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

在等待补参和完成后分别刷新页面，应恢复服务端状态。网络结果不确定时使用“检查原提交”，不要
新建同一个目标。测试中文输入法候选确认不会发送、Shift+Enter 换行，以及窄屏下补参和结果可操作。
已经“已结束”的 Run 不能因修复代码而恢复；排除错误后创建新目标，或使用符合资格的显式重试。

默认 Mock LLM 是固定规则：支持简单单位换算、ZTA35G 缺参询问和 JSON 参数增量，不支持任意中文
提取或上述多工具自然语言编排。其输出只验证协议和界面。确认、拒绝、过期及可重试失败用受控测试
工具验证，默认两个工具不会触发确认。并发 CAS、事务、超时和 Token 预算边界以自动化测试为准。

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

## 当前限制

本项目不提供登录、多用户隔离、Redis、后台 Worker、SSE、WebSocket、文件上传、EBSD 输入、
ML Training、Planner、多 Agent、动态插件上传或生产部署能力。ZTA35G Tool 只适用于 ZTA35G
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

## Agent Loop 升级与验证

Alembic 历史保留，新增 `0015` 建立 Agent 控制表并调整既有约束；迁移不自动清空或回填旧业务数据。
旧 Router、固定解释和补参执行入口已移除。升级前核实本项目数据库、bucket 和资产归属；业务清理由 Conversation 删除流程处理，
不清空共享卷，不触碰 `SEM/`、模型权重或凭据。

离线统一入口：`scripts/acceptance/run-agent-acceptance.ps1 -BackendPython <Python 3.11 路径>`。
该入口运行 Backend、Mock Runtime、前端和脚本门禁，需要本地 PostgreSQL/MinIO；不发起真实 Provider
或 GPU 推理，也不负责启动服务、安装依赖或拉取镜像。
浏览器验证与真实 Provider/GPU 实测分别记录，测试通过不等于服务正在运行。

默认 ZTA35G 单次超时为 1200 秒（配置上限 86400 秒），实际调用仍受 Run 剩余活跃时间限制。
本地 Vite 代理允许 3660 秒请求；没有额外生产反向代理。超时表示停止等待，不表示远端 GPU 已取消。
