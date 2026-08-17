# Materials Agent MVP

这是一个面向材料研究的本地材料智能体 MVP。用户可以通过自然语言创建对话并调用唯一的
`zta35g_sem_virtual_lab` Tool，生成 ZTA35G SEM 图像，并按请求获得力学性能结果和可追溯
解释。

仓库当前代码、配置、迁移和测试是运行行为的事实来源。本项目不维护动态进度 Markdown、
里程碑日志或逐次验收报告；当前状态应直接通过 Git 和测试确认。旧设计、计划和验收记录只
保留在 Git 历史中。

## 当前能力

- 单用户、本地运行的最小聊天界面；
- 对话、消息、任务、ToolRun、ToolResult、资产和解释结果的持久化；
- Mock 或 DeepSeek 聊天编排；首版使用 LangChain，不使用 LangGraph；
- 与 Backend 契约一致的 Mock Runtime；
- Python 3.8 隔离环境中的真实 ZTA35G Runtime；
- PostgreSQL 保存结构化事实，MinIO 保存生成图片；
- 可查询聊天时间线、任务状态、Tool catalog、结果和图片资产；
- 一条 PowerShell 命令启动或停止完整本地栈。

本项目仍是本地 MVP，不提供登录、多用户隔离、Redis、后台 Worker、SSE、WebSocket、多
Tool、用户上传真实 SEM 或生产部署能力。

## 架构

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

Backend 负责公共 API、聊天编排、状态转换和持久化。Runtime 只负责 Tool catalog、ready
检查和一次受控推理；独立 Runtime 是旧模型兼容边界，不代表平台采用通用微服务架构。
Backend 与真实 Runtime 不共享 Python 包。

一次 Tool 请求的主要数据流是：

```text
用户消息 -> LLM 路由/参数提取 -> Task + ToolRun
         -> Runtime execute -> 图片/性能结果
         -> MinIO + PostgreSQL -> timeline/API -> 前端
```

每个 ToolRun 独立拥有自己的图片和性能结果。Runtime execute 失败后 Backend 不自动重试；
显式重试会创建新的 ToolRun 和 seed。

## 目录

| 路径 | 职责 |
|---|---|
| `backend/` | FastAPI 应用、领域/应用/基础设施代码、Alembic 迁移和测试 |
| `frontend/` | Vue 3 + Vite + TypeScript 前端和测试 |
| `mock-runtime/` | Python 3.11 的契约一致 Mock Runtime |
| `zta35g-runtime/` | Python 3.8 的真实 ZTA35G Runtime 适配层和兼容性测试 |
| `environments/` | Backend 与真实 Runtime 的 Conda 环境声明 |
| `scripts/dev/` | 本地栈、范围和 SEM 完整性辅助脚本 |
| `scripts/acceptance/` | 可重复运行的验收与离线检查程序 |
| `docs/acceptance/sem-package-manifest.json` | `SEM/` 文件大小和 SHA-256 机器基线 |
| `SEM/` | 本地只读的外部研究模型包；不进入普通 Git 跟踪 |
| `tmp/` | 被忽略的本地状态、日志和验收输出；不是项目事实来源 |

## 环境要求

- Windows PowerShell 5.1 或兼容 PowerShell；
- Docker Desktop 和 Docker Compose；
- `materialsagent-backend` Conda 环境，Python 3.11；
- Node.js `24.14.x` 与 npm `11.9.x`；
- 使用真实模型时需要 `materialsagent-zta35g` Conda 环境（Python 3.8.20）、兼容 GPU
  和只读 `SEM/ZTA35G_lab` 模型包。

依赖版本分别以这些机器文件为准：

- Backend：`backend/pyproject.toml` 与 `environments/materialsagent-backend.yml`；
- Frontend：`frontend/package.json` 与 `frontend/package-lock.json`；
- 真实 Runtime：`environments/materialsagent-zta35g.yml` 和
  `zta35g-runtime/requirements-win-py38.lock.txt`。

旧模型的 PyTorch、Torchvision、CUDA、NumPy、Joblib 和 scikit-learn 依赖只能存在于
真实 Runtime 环境，不得安装到 Backend 环境。

## 本地配置

把 `.env.example` 复制为被 Git 忽略的根 `.env`，并为本机填写数据库、MinIO 和签名
配置。使用 DeepSeek 时还要设置 `DEEPSEEK_API_KEY`。不要提交真实 `.env` 或在终端、
日志和问题报告中打印 Secret。

固定推理参数不是配置项：

```text
num_samples=1
guide_scale=2.0
timesteps=1000
```

`scripts/dev/local-dev.ps1` 每次启动会在内存中生成独立的 Runtime token，只注入 Backend
与 Runtime 子进程，不写入状态文件。它不会解析、复制、打印或修改根 `.env`。

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

也可以独立组合 `-Runtime Mock|Real` 与 `-Llm Mock|DeepSeek`。真实 Runtime 首次加载较慢
时，可把 ready 等待上限从默认 300 秒调整到 10–900 秒，例如：

```powershell
.\scripts\dev\local-dev.ps1 Start `
  -Runtime Real `
  -Llm Mock `
  -ReadyTimeoutSeconds 600
```

入口依次准备 PostgreSQL/MinIO、数据库迁移和 bucket，再等待 Runtime、Backend 与
Frontend ready。固定回环端口为：

| 服务 | 地址 |
|---|---|
| Frontend | `http://127.0.0.1:3000` |
| Backend | `http://127.0.0.1:8000` |
| Runtime | `http://127.0.0.1:8100` |
| PostgreSQL | `127.0.0.1:5432` |
| MinIO API | `http://127.0.0.1:9000` |
| MinIO Console | `http://127.0.0.1:9001` |

任一固定端口已被未知进程占用时，入口会拒绝启动，不会接管或停止占用者。Compose 使用
固定镜像摘要和 `pull_policy: never`，不会自行拉取或替换镜像。

## 停止

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/dev/local-dev.ps1 `
  -Action Stop
```

停止入口只终止能够通过 PID、启动时间、可执行文件和命令标记证明属于本轮的进程。它只
停止本轮拥有的 Compose 服务，不执行 `down -v`，不会删除数据库 volume、bucket、对象
或日志。不要绕过 ownership 检查按进程名或端口批量终止进程。

## 健康检查

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000/
docker compose -f docker-compose.yml ps
```

主要公共 API 位于 `/api/v1`，包括 health、conversations、tasks、timeline、tools、
tool-results 和 assets。具体请求/响应结构以 FastAPI 路由、Pydantic schema 和契约测试
为准，避免在文档中复制一份容易漂移的完整协议。

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

真实 Runtime 的单元与契约/兼容性测试必须在 `materialsagent-zta35g` 环境中运行；只有明确
需要真实推理时才加载模型或占用 GPU。

检查只读 SEM 包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/dev/check-sem-integrity.ps1
```

检查精确改动范围：

```powershell
& .\scripts\dev\check-scope.ps1 `
  -Label MAINTENANCE `
  -AllowedPath @('README.md', 'AGENTS.md')
```

范围检查只接受仓库相对文件路径，不接受通配符、目录级授权或父目录跳转。任何完成声明都
应基于本轮新执行的相关测试、`git diff --check` 和最终 Git 状态。

## 安全与数据边界

- PostgreSQL 保存结构化业务事实，MinIO 保存图片；日志和公共响应不保存图片 bytes。
- 不记录完整 Prompt、完整 Provider 原始响应、Tensor、Secret、权重路径或内部绝对路径。
- `.env`、数据库、MinIO 数据、生成输出、模型权重、缓存、日志和 `tmp/` 都不得提交。
- `SEM/` 始终只读；完整性脚本只枚举文件并校验大小和 SHA-256，不导入模型代码。
- 外部 LLM、Runtime 与 MinIO 调用不得放在数据库长事务中。

## 常见问题

- `LOCAL_DEV_CONFIGURATION_MISSING name=DEEPSEEK_API_KEY`：在根 `.env` 配置 Key；不要
  把值贴入命令或日志。
- `LOCAL_DEV_PORT_CONFLICT`：确认固定端口占用者；脚本不会替你终止未知服务。
- `LOCAL_DEV_READY_TIMEOUT`：查看本轮 `tmp/local-dev/<run_id>/` 日志；真实 Runtime
  重点检查环境、只读模型根和 GPU。
- `LOCAL_DEV_STATE_REQUIRES_STOP`：先使用正式 Stop 入口清理已记录资源，不要直接删除
  状态文件后宽泛杀进程。
- `LOCAL_DEV_PROCESS_OWNERSHIP_NOT_VERIFIED` 或
  `LOCAL_DEV_DOCKER_OWNERSHIP_NOT_VERIFIED`：脚本无法证明对象属于本轮；核对状态和日志
  后再处理。

## 文档与历史

当前树只保留两份 Markdown：`AGENTS.md` 规定协作和安全规则，`README.md` 说明项目本身。
不要为进度、里程碑、实施计划、验收报告或交接摘要新增 Markdown；这些一次性事实由 Git
提交、测试输出和被忽略的本地证据承担。

需要查看已经删除的旧文档时：

```powershell
git log --all -- docs/progress/phase-1-current-status.md
git show <commit>:docs/progress/phase-1-current-status.md
```

同样方式可查旧设计、计划和验收报告。Git 历史用于追溯“过去为何这样做”，当前行为仍以
现有代码、配置、迁移和测试为准。
