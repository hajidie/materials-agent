# 本地开发模式启动与停止

## 用途与边界

`scripts/dev/local-dev.ps1` 为本地单用户开发和人工浏览器体验启动现有
PostgreSQL、MinIO、Backend、Frontend，以及 Mock 或真实 ZTA35G Runtime。
它不是正式 Stage C Runner，不实现 Provider/Runtime 预算账本、临时
database/bucket、完整 Audit、崩溃强一致恢复或通用进程管理。

入口固定使用以下回环端口：Frontend `3000`、Backend `8000`、Runtime
`8100`、PostgreSQL `5432`、MinIO API `9000`、MinIO Console `9001`。任一端口
已占用时，启动会在创建资源前返回 `LOCAL_DEV_PORT_CONFLICT`，不会复用或停止未知
服务。

## 启动前准备

- Docker Desktop 和 Docker Compose 可用，仓库固定的 PostgreSQL/MinIO 镜像已在
  本机；入口使用 `--pull never`，不会自行拉取或替换镜像。
- `materialsagent-backend` Conda 环境、Frontend `node_modules` 已存在。真实 Runtime
  还要求已验证的 `materialsagent-zta35g` 环境。
- PostgreSQL、MinIO 和 Backend 的普通本地配置继续使用仓库既有的、被 Git 忽略的
  根 `.env`。选择 DeepSeek 时，根 `.env` 必须已有 `DEEPSEEK_API_KEY`。入口通过
  Backend 现有配置加载器做静默预检，不自行解析、复制、打印或修改该文件。
- 不再需要向当前 PowerShell 手动注入 Runtime token 或模型目录。入口为每次启动在
  内存中生成独立的 `ZTA35G_RUNTIME_TOKEN`，只注入 Backend 和 Runtime 子进程；真实
  Runtime 固定使用仓库内已确认的 `SEM\ZTA35G_lab` 作为模型根。

Mock LLM 会显式覆盖 `LLM_ADAPTER=mock` 并向 Backend 传入空白
`DEEPSEEK_API_KEY`；Runtime、Frontend 和 Compose 子进程会移除它们不需要的
Secret。DeepSeek Key 只由 Backend 配置加载器从根 `.env` 读取。Key 和本次生成的
Runtime token 都不会进入命令参数、控制台输出或状态文件。

## 启动

真实 Runtime + DeepSeek 的最短启动方式：

```powershell
.\scripts\dev\local-dev.ps1 Start -Runtime Real -Llm DeepSeek
```

除根 `.env` 中已有 `DEEPSEEK_API_KEY` 外，不需要手动填写任何值。

默认启动 Mock Runtime + Mock LLM：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/dev/local-dev.ps1 `
  -Action Start
```

四种组合通过参数选择：

```powershell
# Mock Runtime + Mock LLM
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/local-dev.ps1 -Action Start -Runtime Mock -Llm Mock

# Mock Runtime + DeepSeek
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/local-dev.ps1 -Action Start -Runtime Mock -Llm DeepSeek

# 真实 Runtime + Mock LLM
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/local-dev.ps1 -Action Start -Runtime Real -Llm Mock

# 真实 Runtime + DeepSeek
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/local-dev.ps1 -Action Start -Runtime Real -Llm DeepSeek
```

启动顺序是 Compose 依赖、Alembic/既有 bucket 准备、Runtime ready、Backend
ready、Frontend ready。默认 ready 上限为 300 秒；真实 Runtime 首次加载较慢时可在
`10`–`900` 秒内调整，例如 `-ReadyTimeoutSeconds 600`。这不改变 Backend 对真实
推理的既有 900 秒上限。Windows PowerShell 5.1 下 Docker 写入 stderr 的正常启动
进度不会被当作失败；入口等待 Compose 结束后以实际退出码判断结果。

成功后输出六个服务的状态和：

```text
Frontend: READY http://127.0.0.1:3000
state=tmp/local-dev/state.json
logs=tmp/local-dev/<run_id>
```

状态文件只含 run id、模式、PID、启动时间、可执行文件、精确命令标记、端口和
Docker context/engine ownership；不含 Token、Key、密码、完整环境或模型根路径。

## 停止

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/dev/local-dev.ps1 `
  -Action Stop
```

停止按 Frontend、Backend、Runtime 的反序处理，并在终止前核对记录的 PID、启动
时间、可执行文件、仓库根和精确命令标记。Compose 只对本轮从停止态启动的
`minio` / `postgresql` 执行 `docker compose stop`；本轮开始前已运行的服务不会被
记录为 owned，也不会被停止。入口从不执行 `down -v`，不删除 database、volume、
bucket、object 或日志。Windows PowerShell 5.1 下 Docker 写入 stderr 的正常停止进度
不会被当作失败；入口等待原生命令结束后以实际退出码判断结果。

重复停止且状态文件不存在时返回 `LOCAL_DEV_NOT_RUNNING`。重复启动同一组合时会
重新探测 ready 并返回 `LOCAL_DEV_ALREADY_RUNNING`；由于 Runtime token 不落盘，
重复启动时通过 Backend 的 Tool catalog 代为验证 Runtime ready。若请求了不同组合，
先执行 Stop，再重新 Start。残缺或不可验证的状态不会被自动接管。

## Ready 检查与故障处理

除入口汇总外，可执行只读检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000/
docker compose -f docker-compose.yml ps
```

常见结果：

- `LOCAL_DEV_CONFIGURATION_MISSING name=DEEPSEEK_API_KEY source=root_dotenv`：在被 Git
  忽略的根 `.env` 中配置 `DEEPSEEK_API_KEY`。输出只含变量名，不含值。
- `LOCAL_DEV_CONFIGURATION_INVALID source=root_dotenv mode=deepseek`：根 `.env` 中存在
  Backend 无法接受的配置；入口不会回显配置值。
- `LOCAL_DEV_PORT_CONFLICT`：先确认占用者；入口没有启动任何资源，也不会代替用户
  终止占用者。
- `LOCAL_DEV_START_FAILED stage=<stage>`：查看输出给出的本轮日志目录。入口会尝试
  清理已记录资源；若返回 `LOCAL_DEV_START_CLEANUP_INCOMPLETE`，保留状态后再次执行
  Stop。
- `LOCAL_DEV_STATE_REQUIRES_STOP`：先执行 Stop。不要删除状态后按名称或端口宽泛杀
  进程。
- `LOCAL_DEV_PROCESS_OWNERSHIP_NOT_VERIFIED` 或
  `LOCAL_DEV_DOCKER_OWNERSHIP_NOT_VERIFIED`：入口已拒绝停止无法证明属于本轮的
  对象；核对 PID/命令、Docker context 和日志，再重试 Stop。
- `LOCAL_DEV_READY_TIMEOUT`：服务进程已启动但未在上限内 ready；真实 Runtime 可先
  查看 `runtime.stderr.log`，确认模型环境、模型根和 GPU 状态，再安全 Stop。

本入口不自动安装依赖、不自动重启崩溃进程、不打开浏览器，也不证明真实
Provider/Runtime 的正式验收质量。真实 DeepSeek、真实 Runtime 和 GPU 仅应由项目
负责人在离线验证完成后亲自启动和体验。
