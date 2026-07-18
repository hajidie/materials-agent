# 阶段 1 当前执行状态

> 本文件只保存当前状态、最近一次完成摘要和下一步。完整历史由未来经项目负责人验收后的 Git commit 保存。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M2：PostgreSQL、MinIO 与基础持久化 |
| 当前工作单元 | M2-B PostgreSQL、SQLAlchemy、Alembic 与 Actor |
| 状态 | `READY_FOR_M2_B` |
| 上一已完成工作单元 | M2-A |
| 当前 branch | `main` |
| Git 事实获取方式 | 本工作单元开始时重新执行 `git branch --show-current`、`git rev-parse HEAD`、`git log -1 --oneline`、`git status --short`、`git diff --check`、`git diff --cached --name-only` |
| 已确认设计基线 | 第一节、第二节、第三节、第四节 A、第四节 B，共五份；本轮未修改 |
| 阶段 1 实施计划路径 | `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`（状态：已确认实施计划；本轮未修改） |
| 是否处于项目负责人暂停点 | 否 |
| 更新时间 | `2026-07-18T16:44:56+08:00` |

## 最近一次完成摘要

- 工作单元边界：M2-A 已通过项目负责人审阅；本收尾对话只修正 `.env.example` 注释、补记完整 M1 回归、建立被 Git 忽略的本地 `.env` 并形成 M2-A 独立 commit。未实现数据库模型、Alembic、Actor、UnitOfWork、Repository、StorageService、Backend 依赖探针或 bucket 初始化，未开始 M2-B、M2-C 或 M3。
- 起点门禁：branch 为 `main`，HEAD 为 `b4931011ddc4e1dcd792067270733edb66117f51`，最近提交为 `b493101 feat: add backend health skeleton`；开始时工作区干净、暂存区为空，六条 Git 门禁命令均符合项目负责人给定预期。
- Docker 能力：Docker Client/Engine 为 `29.6.1`，Docker Desktop 为 `4.82.0 (233772)`，Docker Compose 为 `v5.3.0`，Engine 为 `linux/amd64`；开始前没有容器、Compose project、volume，宿主端口 `5432/9000/9001` 均无监听冲突。
- PostgreSQL 镜像：使用 `postgres:17.10-bookworm@sha256:4f736ae292687621d4dbe0d499ffd024a36bd2ee7d8ca6f2ccd4c800f047b394`；本机标签 RepoDigest 精确包含批准 digest，平台为 `linux/amd64`。固定镜像内的 `pg_isready 17.10` 已用 `--pull never` 实测可用。
- MinIO 镜像：使用 `minio/minio:RELEASE.2025-09-07T16-13-09Z@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e`；本机标签 RepoDigest 精确包含批准 digest，平台为 `linux/amd64`。固定镜像内的 `/usr/bin/curl 8.11.0` 已用 `--pull never` 实测可用。
- Compose 配置：`docker-compose.yml` 的 project name 为 `materialsagent`，service 精确为 `minio,postgresql`，没有第三个 service；两项 image 同时保留批准 tag 与 digest，均设置 `platform: linux/amd64` 和 `pull_policy: never`。两次完整 `docker compose config` 均退出 0，脱敏配置摘要完全一致，SHA-256 均为 `2d30a4700f839e0c42f8f499efb0b6b2a3f8c201a4d80cc856fab1c6c72c43e3`。
- 配置负面测试：在隔离子 PowerShell 中清除必填 `POSTGRES_PASSWORD` 后，`docker compose config --quiet` 退出 1，并明确报告该变量缺失；未启动容器、未创建 volume，仓库状态未变化。该负面测试在本地 `.env` 建立前完成，只使用安全占位值，未记录完整密码。
- 端口与数据：PostgreSQL 为 `127.0.0.1:5432->5432`；MinIO API 为 `127.0.0.1:9000->9000`，Console 为 `127.0.0.1:9001->9001`。数据分别使用 `materialsagent_postgresql_data` 与 `materialsagent_minio_data` 两个稳定命名 volume，没有仓库 bind mount。
- `.env.example`：仅因原文件缺少 Compose 所需的 MinIO API/Console 宿主端口变量而增加 `MINIO_API_PORT` 和 `MINIO_CONSOLE_PORT`；已有 `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY` 映射为容器内 `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`，未重复增加同义凭据变量，未写入本机值或真实 Secret。
- 本地配置连续性：已创建被 Git 忽略的根 `.env`，使用与 M2-A 首次初始化现有 PostgreSQL/MinIO volumes 完全相同的本地配置；未在仓库、本状态文件或汇报中记录凭据值。清除 Shell 同名变量后，`docker compose config --quiet` 退出 0，`docker compose up -d --pull never postgresql minio` 复用两个既有 volume 且未重建；PostgreSQL `pg_isready` 和 TCP 密码认证只读查询成功，MinIO ready 为 HTTP 200。随后 `docker compose down` 退出 0，容器为 0、两个 volume 保留；`.env` 未被跟踪、未暂存、未出现在 Git status。
- 启动与健康：`docker compose up -d --pull never postgresql minio` 退出 0，未执行任何 pull。PostgreSQL 达到 `healthy` 且 `pg_isready` 退出 0；MinIO 达到 `healthy` 且 `/minio/health/ready` 返回 HTTP 200。未创建业务表、schema、migration、bucket 或对象。
- 运行镜像身份：PostgreSQL 容器反查 image ID 为批准的 `sha256:4f736ae292687621d4dbe0d499ffd024a36bd2ee7d8ca6f2ccd4c800f047b394`，RepoDigest 与 `linux/amd64` 均匹配；MinIO 容器反查 image ID 为批准的 `sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e`，RepoDigest 与 `linux/amd64` 均匹配。结论来自运行容器与 image inspect，不只来自 Compose 文本。
- 重启稳定性：`docker compose restart postgresql minio` 退出 0；两个服务重新达到 `healthy`，重启后的 `pg_isready` 再次退出 0，MinIO ready endpoint 再次返回 HTTP 200。
- Backend 边界：未修改 `backend/**`。`backend/tests/api/test_health.py` 单独执行得到 `6 passed in 0.55s`；随后完整重新执行 `backend/tests/unit` 和 `backend/tests/api/test_health.py`，得到 `8 passed in 0.44s`、退出码 0。`/api/v1/health/ready` 仍保持 M1 的 HTTP 503、`NOT_READY`，尚未接入 PostgreSQL/MinIO 探针。
- 停止与保留：验收后执行不带 `-v` 的 `docker compose down` 并退出 0；本项目运行容器和已停止容器均为 0，`5432/9000/9001` 无遗留监听；两个命名 volume 均保留，未执行 volume/image/system prune。
- 范围检查：M2 修改前为 `SCRIPT_CONFIGURATION_ERROR`/6；加入 M2-A 精确 allowlist 后为 `SCOPE_OK M2`/0。临时根文件得到 `OUT_OF_SCOPE_CHANGES M2`/5 后已删除；M3 继续为 `SCRIPT_CONFIGURATION_ERROR`/6。M0、M1 规则及全局 current status 允许路径保持不变。
- SEM 与 Git 边界：`scripts/dev/check-sem-integrity.ps1` 为 `SEM_INTEGRITY_OK`，仍为 57 个文件、2,043,071,133 字节、aggregate fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。`git diff --check` 退出 0；本地 `.env` 被正确忽略，没有临时日志、临时 Compose 副本或 Backend/M2-B/M2-C 文件，暂存区为空。
- 镜像操作：本轮未执行 `docker pull`、`docker compose pull`、镜像删除、重新打标签或 prune；所有临时能力检查和正式启动均使用 `--pull never`，未切换镜像版本。
- 已知风险：两个命名 volume 保留了使用安全本地配置初始化的服务数据；PostgreSQL 初始化凭据只在空数据目录首次初始化时生效。被忽略的本地 `.env` 已固定同一套配置并完成复用验证，不得删除、提交或用不同凭据覆盖。M2-B 尚未开始，Backend ready 尚不能证明依赖可用。
- 阻塞事项：无。M2-A 已通过项目负责人验收并准备形成独立 commit。

## 下一步

M2-A 已通过项目负责人验收并准备形成独立 commit。只有 commit 成功且工作区干净后，才允许在新的 Codex 对话中开始 M2-B。M2-B 当前尚未开始。
