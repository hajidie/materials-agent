# 阶段 1 当前执行状态

> 本文件只保存当前状态、最近一次工作单元摘要和下一步。完整历史由未来经项目负责人验收后的 Git commit 保存。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M2：PostgreSQL、MinIO 与基础持久化 |
| 当前工作单元 | M2-C MinIO、StorageService 与依赖 ready 探针 |
| 状态 | `READY_FOR_M2_C` |
| 上一已完成工作单元 | M2-B |
| 当前 branch | `main` |
| M2-B 起点 HEAD | `1e9313b61a4bee5689dd5eaa5930eaf4c9f608be` |
| 已确认设计基线 | 第一节、第二节、第三节、第四节 A、第四节 B，共五份；本轮未修改 |
| 阶段 1 实施计划 | `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`；本轮未修改 |
| 是否处于项目负责人暂停点 | 否 |
| 更新时间 | `2026-07-18T19:31:07+08:00` |

## M2-B 最近完成摘要

- 验收、起点与范围：M2-B 已通过项目负责人验收。本工作单元从 `main` / `1e9313b61a4bee5689dd5eaa5930eaf4c9f608be` 开始，开始时工作区干净、暂存区为空。SQLAlchemy、Alembic、Actor、Repository、UnitOfWork 和幂等 bootstrap 已完成；未实现 MinIO SDK、StorageService、bucket 初始化或 ready 依赖探针，M2-C 尚未开始，未进入 M3。
- 本地配置：根 `.env` 仍存在、由 `.gitignore` 命中、未被 Git 跟踪或显示在 status。Backend 与 Alembic 均通过同一 `load_settings()` 读取根 `.env`；显式 mapping 和直接 `AppSettings` 构造保持测试隔离。未复制或记录 `.env` 值。
- 依赖：先执行 pip dry-run，再以 `backend/pyproject.toml` 为唯一事实来源安装。新增直接依赖及实际版本为 SQLAlchemy `2.0.51`、Alembic `1.18.5`、psycopg/psycopg-binary `3.3.4`、pydantic-settings `2.14.2`。未做全环境升级，未使用 force-reinstall/ignore-installed/no-deps/prune，未删除环境中既有包。`pip check` 返回 `No broken requirements found.`
- SQLAlchemy：使用同步 SQLAlchemy 2.x 和同步 psycopg 3，与已确认基线无冲突，并避免在当前单实体 MVP 引入 async engine/session 复杂度。模块 import 不创建全局 engine/Session、不连接数据库、不运行 migration 或 bootstrap。
- 连接安全：PostgreSQL URL 使用 SQLAlchemy `URL.create` 根据五个配置项构造，特殊字符由库处理；engine 显式创建且设置 `hide_parameters`，不记录密码或完整 URL。错误端口子进程返回稳定 `DatabaseUnavailableError:Database unavailable.`，输出不含密码、完整 URL、`.env`、SQL、绝对路径或堆栈。
- Alembic：基线只有 `backend/alembic/versions/0001_create_actor.py`；`heads` 和主库 `current` 均为 `0001_create_actor (head)`，`alembic check` 为 `No new upgrade operations detected.`。主开发库只执行 `upgrade head`；`downgrade base → upgrade head` 只在一次性测试库中验证。
- 主库事实：迁移前 public schema 无表；迁移后表精确为 `actor`、`alembic_version`。未出现 Conversation、Message、Task、Asset、ToolRun 或 ToolResult 等未来表。PostgreSQL 复用既有 `materialsagent_postgresql_data` volume，未删除、重建或重新初始化；MinIO 本轮未启动。
- Actor 契约：`actor_id` 为 opaque text 主键且非空；`user_id` 可空、无 User 外键、无唯一约束；`actor_origin` 为非空短文本且 MVP 限定 `LOCAL_ANONYMOUS`；`created_at` 为非空带时区 UTC；`linked_at` 可空。未增加认证、权限、租户、计费、metadata JSONB 或软删除字段。Domain Actor 是纯 Python dataclass，不依赖 SQLAlchemy、FastAPI 或 PostgreSQL。
- Repository/UoW：Actor Repository 只负责 `get/add`，不自行 commit；SQLAlchemy UnitOfWork 在上下文中创建短生命周期 Session，统一 commit/rollback/close，不存在全局长生命周期 Session。故障注入测试确认异常后 Actor 不落库、Session 关闭且后续事务可用。
- UnitOfWork 审查修复：`commit()` 在 `IntegrityError` / `DBAPIError` / 其他 `SQLAlchemyError` 后统一复用已脱敏的 `rollback()` 路径，不再直接调用底层 `session.rollback()`。即使二次 rollback 自身失败，`DBAPIError` 也只转换为 `DatabaseUnavailableError("Database unavailable.")`，其他 `SQLAlchemyError` 只转换为 `PersistenceError("Persistence operation failed.")`；不泄露原始 SQL、参数、密码、URL、驱动或内部异常文本。聚焦 fake Session 测试先以 2 failed 证明原始异常逃逸，最小修复后为 2 passed，并确认上下文退出后 Session 仍 close/清空；普通业务异常和正常 commit 语义未改变。
- bootstrap：`LOCAL_ACTOR_ID` 只来自 Backend 配置层的根 `.env`/环境变量，未记录其实际值，不接受客户端 `user_id`。主库连续调用两次返回同一 actor_id，`created_at` 未覆盖，最终 Actor 只有 1 行，`actor_origin=LOCAL_ANONYMOUS`、`user_id/linked_at` 为空。两线程真实主键竞争测试强制两个事务先同时观察到缺行，冲突被安全回滚并读回胜出行，最终仍只 1 行且不外泄 IntegrityError。
- 测试与隔离：严格先红测，首次因 Alembic 尚未安装而预期失败；UnitOfWork 审查修复的两个聚焦测试也先证明二次 rollback 原始异常逃逸，再转绿。最终 M2-B 数据库套件为 `17 passed`、退出 0，M1 完整回归为 `8 passed`、退出 0。测试库使用仅含安全字符的 `materialsagent_test_<random>` 名称，以 autocommit 创建/删除，fixture 最终终止本轮连接并删库；最终只读查询确认遗留数为 0。
- 安全与回归：单独 import `config`、`db.session`、`application.bootstrap` 在错误数据库配置子进程中仍成功，不自动连接、迁移或写 Actor。M1 `/live` 和 `/ready` 未修改，回归仍为 8 passed。
- 范围与 SEM：M2 allowlist 已替换为本轮精确文件，不使用 `backend/**` 或 migration wildcard，不允许 `docker-compose.yml`/`.env.example`。正面检查为 `SCOPE_OK M2`；临时范围外根文件返回 `OUT_OF_SCOPE_CHANGES M2`/5 后已删除；M3 仍为 `SCRIPT_CONFIGURATION_ERROR`/6。`SEM_INTEGRITY_OK`：57 个文件、2,043,071,133 字节、aggregate fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- 停止与保留：验证后执行不带 `-v` 的 `docker compose down`，容器为 0、宿主 `5432` 监听为 0；`materialsagent_postgresql_data` 和 `materialsagent_minio_data` 均仍存在。未执行 pull、volume/image/system prune、主库 downgrade、drop table 或 drop database。
- Git 边界：M2-B 已通过项目负责人验收；批准提交范围精确为 18 个 M2-B 文件，不包含 `.env`、Compose 变更、M1 文件、M2-C 或 M3 文件。
- 已知风险：首次 migration 和稳定 Actor 已写入保留的主开发数据库 volume，该结构和行是已验收事实，不得通过删 volume、主库 downgrade 或手工 drop 回退。根 `.env` 必须继续保留与 M2-A 初始化 volume 相同的配置。Actor origin 的当前 CHECK 只允许 MVP `LOCAL_ANONYMOUS`；未来真实认证来源必须通过新 migration 显式演进，不应修改历史 migration。Backend ready 仍不能证明 PostgreSQL/MinIO 依赖就绪，该工作与 MinIO/StorageService 全部留到 M2-C。

## 下一步

M2-B 已通过项目负责人验收，状态为 `READY_FOR_M2_C`，但 M2-C 尚未开始。必须在新的 Codex 对话中重新恢复 Git、Docker、根 `.env` 和两个既有 volume 的实际状态后，才能执行 M2-C；本轮提交完成后立即停止，不进入 M2-C 或 M3。
