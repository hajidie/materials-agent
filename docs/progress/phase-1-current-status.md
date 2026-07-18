# 阶段 1 当前执行状态

> 本文件只保存当前状态、最近一次完成摘要和下一步。完整历史由未来经项目负责人验收后的 Git commit 保存。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M2：PostgreSQL、MinIO 与基础持久化 |
| 当前工作单元 | 尚未开始 |
| 状态 | `READY_FOR_M2` |
| 上一已完成工作单元 | M1 |
| 当前 branch | `main` |
| Git 事实获取方式 | 本工作单元开始时重新执行 `git branch --show-current`、`git rev-parse HEAD`、`git status --short`、`git log -1 --oneline`、`git diff --check`、`git diff --cached --name-only` |
| 已确认设计基线 | 第一节、第二节、第三节、第四节 A、第四节 B，共五份；本轮未修改 |
| 阶段 1 实施计划路径 | `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`（状态：已确认实施计划；本轮未修改） |
| 是否处于项目负责人暂停点 | 否 |
| 更新时间 | `2026-07-18T13:12:22+08:00` |

## 最近一次完成摘要

- M1 已通过项目负责人验收。Backend 最小应用、live、ready、404、request_id 和结构化日志已完成；正式 Conda 环境使用 Python `3.11.15`，现有环境经审计后复用，未发现旧模型依赖。
- 一次性干净 Python 3.11 venv 已证明仓库依赖声明完整，临时 venv 已删除；8 个测试在正式环境和干净环境中均通过。
- 起点门禁：branch 为 `main`，HEAD 为 `4aacc2dea0e73b8cdb4671fb7d304327f8bfd470`，最近提交为 `4aacc2d chore: prepare phase 1 repository conventions`；开始时工作区干净、暂存区为空，六条 Git 门禁命令均退出 0。
- 现有环境审计：`D:\ProgramData\Anaconda3\envs\materialsagent-backend` 存在，并被 `D:\ProgramData\Anaconda3\Scripts\conda.exe` 的 `conda env list` 和 JSON 输出识别；prefix 为该目录，Python 可执行文件位于该 prefix，Python 为 `3.11.15`，pip 为 `26.1.2`，Python、pip 和 `pip check` 均可正常运行。
- 环境边界：审计到 31 个 Python 分发；未发现 `torch`、`torchvision`、`torchaudio`、CUDA/PyTorch-CUDA、NumPy、Joblib、scikit-learn/sklearn 或其他明显旧 ZTA35G 模型依赖，对应 import spec 也均不存在。现有额外包包括 `pydantic-settings 2.14.2`、`python-dotenv 1.2.2`、`httpx2/httpcore2 2.7.0` 等；它们未写入项目直接依赖、未批量卸载，`pip check` 未发现冲突。
- 环境复用决定：满足 Conda 可识别、Python 3.11.x、Python/pip 可运行、prefix 匹配、无旧模型依赖、无明显损坏六项条件，因此复用原 `materialsagent-backend`。本轮未创建、删除、覆盖、重建或重命名环境，未改变 Python 主/次版本，未使用 `--prune`、`--force-reinstall`、`--ignore-installed`、`--no-deps` 或全环境升级，也未批量卸载包。
- 依赖唯一事实来源：新增 `environments/materialsagent-backend.yml`，只声明环境名、`python=3.11` 和 `pip`；新增 `backend/pyproject.toml`，直接运行依赖为 `fastapi==0.139.2`、`pydantic==2.13.4`、`uvicorn==0.51.0`，`dev` 依赖为 `httpx==0.28.1`、`pytest==9.1.1`。五个直接依赖安装前后版本一致。
- 安装：`conda run -n materialsagent-backend python -m pip install --dry-run -e "backend[dev]"` 只计划重装本项目 editable 分发，没有升级、降级或新增其他包。默认 `conda run` 的实际安装子进程完成后，Conda 25.5.1 在把含中文路径的输出转回 GBK 控制台时发生包装层 `UnicodeEncodeError` 并返回 1；随后用同一目标环境的 `D:\ProgramData\Anaconda3\envs\materialsagent-backend\python.exe -m pip install -e "backend[dev]"` 等价命令重新执行，退出 0，只卸载并重装同版本本项目分发，最终 `pip check` 退出 0。
- 仓库文件：修改 `scripts/dev/check-scope.ps1`；新增环境文件、`backend/pyproject.toml`、`main.py`、`config.py`、`logging.py`、`health.py`、`backend/tests/conftest.py`、`backend/tests/unit/test_config.py` 和 `backend/tests/api/test_health.py`。Python 3.11 namespace package 导入和 editable 安装均已验证，因此未创建四个可选 `__init__.py`；它们仍在 M1 allowlist 中逐一路径列明。
- Backend 行为：`create_app() -> FastAPI` 在 import 时不连接 PostgreSQL、MinIO、LLM、Runtime 或 `SEM/`。`GET /api/v1/health/live` 返回 HTTP 200、`LIVE` 且字段精确为 `status/checked_at/request_id`；`GET /api/v1/health/ready` 不执行外部探测，返回 HTTP 503、`NOT_READY` 和同一安全三字段结构；ready 失败后 live 仍为 200/LIVE；未知路径返回标准 `404 {"detail":"Not Found"}`。
- 配置与日志：M1 只解析带安全默认值的 `APP_ENV` 和 `LOG_LEVEL`，未来依赖变量缺失不阻止 live；非法配置统一抛出不含输入值的 `Invalid application configuration.`。每个请求生成独立 `req_...`，JSON 请求完成日志包含 `timestamp/level/event/request_id/method/path/status_code/duration_ms`，不记录 Header、正文、Secret、环境变量值、绝对路径或异常文本。
- 测试先行：实现前运行规定 pytest 命令得到 8 failed、退出 1，失败原因均为目标模块尚不存在；实现后得到 8 passed、退出 0。最终使用 `conda run --no-capture-output -n materialsagent-backend python -m pytest backend/tests/unit backend/tests/api/test_health.py -q` 绕过上述 Conda 输出捕获问题，得到 `8 passed in 0.43s`、退出 0。
- scope：修改前 M1 为 `SCRIPT_CONFIGURATION_ERROR`/6；加入精确 allowlist 后为 `SCOPE_OK M1`/0。临时创建根 `m1-scope-negative.tmp` 得到 `OUT_OF_SCOPE_CHANGES M1`/5 后已立即删除；M2 仍为 `SCRIPT_CONFIGURATION_ERROR`/6。M0 allowlist、既有退出码和全局 current status 允许路径保持不变。
- SEM 完整性：`scripts/dev/check-sem-integrity.ps1` 退出 0/`SEM_INTEGRITY_OK`；仍为 57 个文件、2,043,071,133 字节、aggregate fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`，未加载、运行或修改 `SEM/`。
- 人工验收：Uvicorn 使用目标环境启动在 `127.0.0.1:8000`。两次 live 均为 HTTP 200，request_id 分别为 `req_93d7660a3a1049dbbc68bb1cb3cba1d4` 和 `req_7763ac082b5f44d9ae174df36f393c17`；ready 为 HTTP 503/`NOT_READY`；未知路径为 HTTP 404/`{"detail":"Not Found"}`。一条对应 live 的安全 JSON 日志为 `{"timestamp":"2026-07-18T03:43:00.498663Z","level":"INFO","event":"http_request_completed","request_id":"req_6a107d7fc31845079ae0593083552ef0","method":"GET","path":"/api/v1/health/live","status_code":200,"duration_ms":5.775}`。验收 PID 已停止，端口 8000 监听数为 0，临时日志文件已删除。
- Git 范围：本轮只包含上述 M1 文件和本动态进度文件；五份设计基线、阶段 1 计划、根 `AGENTS.md`、`.env.example`、`.gitignore`、SEM manifest、SEM 完整性脚本及 M0 其他稳定文件未修改。暂存区保持为空。
- 已知风险：当前 PowerShell 的 `PATH` 未初始化裸 `conda` 命令，需使用标准启动器绝对路径或先由用户正常初始化 Conda；Conda 25.5.1 默认捕获含中文路径的子进程输出时可能发生 GBK 转码错误，`conda run --no-capture-output ...` 已验证可稳定返回真实测试输出。现有额外 Python 包未被清理，虽 `pip check` 正常且无旧模型依赖，后续里程碑仍应在安装前继续做 dry-run/兼容性审计。M1 的 ready 按设计固定未就绪，尚不能证明 PostgreSQL、MinIO、LLM 或 Runtime 可用。
- 阻塞事项：无。M1 已通过项目负责人验收；M2 尚未开始。

## 下一步

M2 尚未开始。必须在新的 Codex 对话中重新检查 Git 状态后，才能执行 M2。
