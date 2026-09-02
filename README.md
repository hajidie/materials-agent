# Materials Agent MVP

这是一个面向材料研究的本地材料智能体 MVP。用户通过自然语言创建对话，调用当前默认应用
组合中唯一注册的 `zta35g_sem_virtual_lab` Tool，根据 ZTA35G 热处理工艺参数生成 SEM 图像，
并按请求获得力学性能结果和可追溯解释。

## Quick Context

| 项目维度 | 当前事实 |
|---|---|
| 项目类型 | 单用户、本地运行的材料研究智能体；Backend 是模块化单体，模型执行在隔离 Runtime 中 |
| 当前阶段 | 可运行 MVP，不是生产部署方案 |
| 主要能力 | 自然语言输入 → 受控 Tool 路由与补参 → ZTA35G SEM 生成 → 可选力学性能与解释 |
| 当前 Tool | 默认应用组合只注册 `zta35g_sem_virtual_lab` |
| 扩展底座 | 显式、进程内 Tool Registry 与 Router；新 Tool 需要显式定义、适配器和隔离边界 |
| 数据存储 | PostgreSQL 保存结构化事实，MinIO 保存生成图片 |
| 明确不包含 | 登录、多用户、Redis、后台 Worker、SSE/WebSocket、上传真实 SEM、多 Agent、动态插件和生产部署 |

仓库当前代码、配置、迁移、测试和实际运行行为是事实来源。`AGENTS.md` 规定 Coding Agent 的
工作规则；`docs/project-context.md` 记录长期架构、Tool 机制和状态模型。旧设计、计划和验收
记录只在 Git 历史中追溯，不作为当前合同。

## 当前能力

- Vue 3 + Vite 的本地聊天界面；
- 对话、消息、任务、输入 Revision、ToolRun、ToolResult、资产和解释结果持久化；
- Mock 或 DeepSeek 聊天编排；当前使用 LangChain，不使用 LangGraph；
- LLM 候选提议、Registry 解析与授权、固定 Tool 补参组成的自然语言单 Tool 链路；
- 与 Backend 合同一致的 Mock Runtime；
- Python 3.8 隔离环境中的真实 ZTA35G Runtime；
- PostgreSQL 保存结构化事实，MinIO 保存生成图片；
- 可查询聊天时间线、任务状态、Tool catalog、结果和图片资产；
- 完成一次性环境准备后，通过一个 PowerShell 入口启动或停止完整本地栈。

测试中的 ML Training 风格 Tool 只验证 Registry 与 Router 能承载异构合同，不会训练模型、
读取数据文件或进入默认应用组合。

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

Backend 负责公共 API、聊天编排、Tool Registry、Tool catalog、状态转换和持久化。Runtime
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

把 `.env.example` 复制为被 Git 忽略的根 `.env`，并为本机填写数据库、MinIO 和签名配置。
使用 DeepSeek 时还要设置 `DEEPSEEK_API_KEY`。不要提交真实 `.env`，也不要在终端、日志或
问题报告中打印 Secret。

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

真实 Runtime + DeepSeek：

```powershell
.\scripts\dev\local-dev.ps1 Start -Runtime Real -Llm DeepSeek
```

也可以独立组合 `-Runtime Mock|Real` 与 `-Llm Mock|DeepSeek`。真实 Runtime 首次加载较慢时，
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

主要公共 API 位于 `/api/v1`，包括 health、conversations、tasks、timeline、tools、
tool-results 和 assets。具体请求/响应结构以 FastAPI 路由、Pydantic schema 和合同测试为准，
不要在文档中复制一份容易漂移的完整协议。

## 测试与检查

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

## 安全与数据边界

- PostgreSQL 保存结构化业务事实，MinIO 保存图片；日志和公共响应不保存图片 bytes。
- 不记录完整 Prompt、完整 Provider 原始响应、Tensor、Secret、权重路径或内部绝对路径。
- `.env`、数据库、MinIO 数据、生成输出、模型权重、缓存、日志和 `tmp/` 都不得提交。
- `SEM/` 始终只读；完整性脚本只枚举文件并校验大小和 SHA-256，不导入模型代码。
- 外部 LLM、Runtime 与 MinIO 调用不得放在数据库长事务中。
- 不混合不同 ToolRun 的资产与结果；显式重试创建新的 ToolRun 和 seed。

## 当前限制

本项目不提供登录、多用户隔离、Redis、后台 Worker、SSE、WebSocket、文件上传、EBSD 输入、
ML Training、Planner、多 Agent、动态插件上传或生产部署能力。ZTA35G Tool 只适用于 ZTA35G
钛合金；不支持上传真实 SEM 后预测；只请求力学性能时仍会生成中间 SEM。

## 常见问题

- `LOCAL_DEV_CONFIGURATION_MISSING name=DEEPSEEK_API_KEY`：在根 `.env` 配置 Key，不要把值
  贴入命令或日志。
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
- `docs/project-context.md`：长期架构、数据流、Tool 机制、状态与设计边界。

不要新增动态进度、里程碑日志、一次性交接摘要或逐次验收报告。当前状态通过 Git、测试和
现场运行检查确认。需要查看已经删除的旧文档时：

```powershell
git log --all -- <旧文档路径>
git show <commit>:<旧文档路径>
```

Git 历史用于追溯“过去为何这样做”，当前行为仍以现有代码、配置、迁移、测试和运行结果为准。
