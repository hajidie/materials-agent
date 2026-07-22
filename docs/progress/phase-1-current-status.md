# 阶段 1 当前执行状态

> 本文件只保存当前动态状态、最近工作单元证据和下一步。产品与架构语义仍以五份已确认设计基线和阶段 1 实施计划为准。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M6 |
| 当前工作单元 | Asset 生命周期、严格图片校验、正式 PNG、MinIO 和受控 Asset API |
| 状态 | `READY_FOR_M7` |
| 上一已完成工作单元 | M6 唯一验收提交（`feat: add generated asset lifecycle`） |
| M5 | 已验收；Runtime 成功后的 ToolRun 继续保持 `RUNNING` |
| M6 | 已通过项目负责人最终代码审查；Critical、Important、Minor 均为 0，已批准唯一验收提交 |
| M7 | 尚未开始；没有 ToolResult、Explanation、selected reference 或 Task 成功聚合 |
| 实际起点 branch / HEAD | `main` / `8d75ad4974711880452b1c32384e6f34301bb3d7` |
| 起始工作区 | clean，暂存区为空，无 untracked 文件 |
| 当前工作区 | M6 的 33 个 allowlist 路径已纳入唯一验收提交；工作区和暂存区均 clean |
| 已确认设计基线 | 五份均未修改 |
| `SEM/` | 未读取内容、未修改、未运行真实模型；只执行完整性哈希检查 |
| 是否处于项目负责人暂停点 | 是 |
| 更新时间 | `2026-07-22T14:33:13+08:00` |

## M6 已完成内容

- 新增 `Asset` Domain、Repository、UoW 接口/实现和唯一新迁移 `0006_asset`；状态限定为 `PENDING`、`AVAILABLE`、`FAILED`、`ORPHANED`，来源字段和 object key 在状态迁移中保持不可变。
- Asset 使用简单主键/外键和必要唯一约束；Application 在 Tx1 同时校验 Actor、Task、M5 `RUNNING` ToolRun、output summary 和图片角色，拒绝跨 Actor、跨 Task、跨 ToolRun 或未记录图片来源。
- 新增独立 ImagePayload 解码器：严格 Base64、4 MiB 原始 `.npy` 上限、重新计算 SHA-256、`np.load(..., allow_pickle=False)`、完整消费 bytes；只接受 `<f4`、二维 `(512, 512)`、C contiguous、有限值且全体位于 `[-1, 1]`。不改 dtype、不转置/reshape、不 clip、不替换 NaN/Inf、不写临时文件。
- 新增 Backend 正式 PNG 编码：`q=floor(((x+1)/2)*255+0.5)`，输出 512×512、Pillow mode `L`、8-bit、无 alpha、`image/png`；固定输入的 PNG SHA-256 为 `f7ab771d76dd6eea15b9df201b81c7286624c831bacf71aa1a4fd55894bfa269`，大小 1480 bytes。
- Backend 正式依赖新增 `numpy==2.4.6`、`Pillow==12.3.0`；未引入 OpenCV、SciPy 或 Runtime 旧模型依赖。
- StorageService/MinIO 最小扩展支持受控对象元数据；同 key 幂等重放同时核对 bytes SHA-256、长度、MIME 和元数据。MinIO ETag 不作为完整性事实。
- 默认应用装配使用惰性 MinIO Adapter：`create_app()` 本身不做外部 I/O，第一次 Asset 存储操作才初始化并幂等确认 bucket；测试仍可注入 StorageService。
- M5 `ToolExecutionService` 增加内部执行回执，把同一次 Runtime 返回的 `ToolExecutionOutput` 以内存对象交给 M6；公共兼容方法仍只返回 ToolRun。Base64、`.npy` bytes 和数组不写 ToolRun、数据库或日志。
- 开发验收 POST 在同一次显式执行中形成 1 个新 ToolRun 和对应 Asset；Adapter execute 精确 1 次，没有自动重试或第二次 Runtime 调用。
- 新增 `GET /api/v1/assets/{asset_id}` 和 `GET /api/v1/assets/{asset_id}/content`；内容只允许 `AVAILABLE`，支持 `inline` / `attachment`，返回固定安全文件名并重新核对 HEAD、长度和 SHA-256。不存在与无权访问统一 404，非 AVAILABLE 为 409，存储不可用为 503。
- Asset 公共投影不含 object key、bucket、MinIO endpoint、Secret、Base64、`.npy`、图片 bytes、Runtime diagnostics 或内部路径。

## M6 项目负责人审查聚焦修订

1. 所有 Storage GET 改为必须显式传入 `max_bytes`；MinIO Adapter 只调用一次 `read(max_bytes + 1)`。Asset 正式 PNG 设计上限固定为 1 MiB（1,048,576 bytes）：AVAILABLE 先以数据库 `size_bytes` 和 1 MiB 上限校验 HEAD，再按数据库大小 bounded GET；PENDING/ORPHANED 恢复固定按 1 MiB bounded GET。
2. 补偿删除增加完整所有权门禁：只有 `asset-id`、`operation-id`、`producer-tool-run-id` 三项均存在且精确匹配当前 Asset 才允许 delete；任一缺失、外部值或无法安全解析均转 ORPHANED，delete 调用数为 0。
3. MinIO 自定义来源元数据扩展为 `asset-id`、`operation-id`、`producer-tool-run-id`、`tool-version`、可选 `model-bundle-id`、`source-npy-sha256`。PENDING/ORPHANED 恢复重新读取 ToolRun 图片摘要，要求来源元数据、HEAD、payload SHA/长度和 PNG/L/512×512 全部一致后才转 AVAILABLE。
4. ToolRun `output_summary` 的每张安全图片摘要新增 `image_role`、`requested_output`、`.npy` SHA-256、`encoding`、`shape`；仍不保存 Base64、`.npy` bytes、数组或 PNG bytes。Asset Tx1 前要求整个安全摘要和 model bundle 与同一次内存 Receipt 精确一致。
5. 新增 `StorageIntegrityError`：对象 sha256/size/content-type/自定义元数据损坏和 bounded-read 超限归入 integrity；网络、超时、服务停止继续归入 `StorageUnavailableError`。AVAILABLE integrity 失败转 ORPHANED 并返回 409；unavailable 返回 503 且数据库状态不变。
6. 开发执行路由把 AssetService 改为 Runtime 前的必需依赖；缺失时 HTTP 503，Runtime 调用、ToolRun 行和 Asset 行均为 0。ToolRun 查询的 `asset_ids` 由同一数据库 UoW 查询，不依赖 MinIO/AssetService 是否可用。

测试先行证据：首次新增/同步修订审查测试运行分别为非 API `28 failed / 46 passed`、API `5 failed / 9 passed`，合计观察到 33 个预期失败实例；最小修复后负责人审查专项为 `28 passed`。

### 最终代码复审小型聚焦修订

1. 新增唯一应用层 `normalize_tool_output_summary()`：ToolExecutionService 持久化与 AssetService Tx1 Receipt 绑定都调用同一函数。`PARTIALLY_SUCCEEDED` 的 Runtime 原始 `safe_message`、`failed_step`、`details` 和 traceback 不进入 ToolRun；错误只保存 Backend 映射后的固定 code/message 和受控 retryable。
2. 新增部分成功端到端覆盖：请求 `sem_image + mechanical_properties`，仅 SEM 完成且性能失败时，Runtime execute 精确 1 次；ToolRun 保持 `RUNNING`，Task 保持 `FAILED/TOOL_UNAVAILABLE`，selected references 为空，不存在 ToolResult/Explanation；Asset 在外部 put 前已提交 `PENDING`，最终 PNG 为 `AVAILABLE`。真实图片 SHA、role、shape 不匹配仍在 Tx1 前拒绝。
3. bounded GET 增加统一硬上限 `MAX_STORAGE_GET_BYTES = 1,048,576` bytes；`max_bytes` 只接受正整数，bool、0、负数和超上限均在 MinIO `get_object` 前以安全错误拒绝；合法上限仍只读取 `max_bytes + 1`。Asset AVAILABLE 与恢复路径继续保持既有 1 MiB 上限。

本轮测试先行证据：首次非 API 红灯为 `3 failed / 4 passed`，精确暴露摘要冲突、0 和超硬上限越过边界；数据库级部分成功红灯为 `1 failed` 且返回 409 `RESOURCE_CONFLICT`。最小修复后合并专项为 `14 passed`。

### 项目负责人最终代码审查与验收结论

- `M6_CODE_REVIEW: APPROVED`；Critical、Important、Minor 均为 0；Decision 为 `APPROVED_FOR_ACCEPTANCE_COMMIT`。
- 最终验证为完整 Backend `563 passed`、Mock Runtime `11 passed`，两个测试根合计 574 个不重复测试。
- 部分成功输出已验证：Runtime execute 精确 1 次；ToolRun 保持 `RUNNING`；Task 保持 `FAILED/TOOL_UNAVAILABLE`；SEM Asset 为 `AVAILABLE`；Runtime 原始错误文本未持久化。
- Storage GET 受控硬上限为 1,048,576 bytes；Alembic head 为 `0006_asset`；Scope、SEM 和 diff 检查均通过。
- M7 尚未开始；本次验收提交未 push、未 amend。

## PostgreSQL / MinIO 固定事务边界

1. Tx1：短事务校验 owned Task/ToolRun/图片来源，生成稳定 `asset_id`、operation id 和 `assets/{environment}/{asset_id}.png`，创建 `PENDING` 并提交。
2. 关闭 UoW 后在事务外严格解码 `.npy`、编码 PNG、执行 MinIO put 和 HEAD；任何图片 bytes 或外部调用都不处于数据库事务内。
3. put/HEAD 一致后进入 Tx2，以条件更新或行锁把同一 Asset 从 `PENDING`/`ORPHANED` 更新为 `AVAILABLE`，写入最终 PNG SHA-256、大小、宽高、位深、MIME、编码摘要和 available 时间。
4. 只有 Tx2 commit 成功才返回 Asset 成功；Task 和 ToolRun 不被 M6 终结，selected references 仍为空。

## 失败、补偿和恢复事实

| 情况 | PostgreSQL 事实 | MinIO 事实 / 恢复 |
|---|---|---|
| `.npy`、hash、dtype、shape、order、有限值或范围非法 | `FAILED / INVALID_MODEL_OUTPUT` | 不 put |
| PNG 编码失败 | `FAILED / PNG_ENCODING_FAILED` | 不 put |
| 非 unavailable 的 put 冲突且 HEAD 确认不存在 | `FAILED / ASSET_UPLOAD_FAILED` | 无对象 |
| MinIO 网络/超时/服务停止 | 数据库当前状态不变；返回 503 | 不猜测对象结果、不补偿删除 |
| put/HEAD 不一致，补偿 delete 成功 | `FAILED / ASSET_UPLOAD_FAILED` | 对象删除 |
| 对象归属冲突或补偿 delete 失败 | `ORPHANED`，保存小型安全原因 | 不伪造 AVAILABLE；保留受控恢复锚点 |
| MinIO 成功但 Tx2 commit 失败 | 保持真实 `PENDING` | 同一 object key 的对象保留；内部 `recover()` 通过 HEAD/get/PNG/hash 校验恢复，不再次 put、不重跑 Runtime |
| 失败状态提交本身失败 | 保持数据库实际状态并返回安全内部/持久化错误 | 不声称 FAILED/ORPHANED 已提交 |
| AVAILABLE 内容缺失或完整性不一致 | 条件更新为 `ORPHANED` 后返回 409 | 不返回 200 假图片 |
| MinIO 暂时不可用 | AVAILABLE 事实不被猜测覆盖，返回 503 | 恢复服务后可再次读取 |

自动化故障注入已覆盖正常 AVAILABLE、三类 FAILED、HEAD 不一致、补偿删除失败、Tx2 commit 失败后 PENDING 恢复、ORPHANED→AVAILABLE、等价恢复不重复 put、条件更新冲突、来源隔离和失败事实 commit 失败。

## API 与 Actor 所有权

- Asset metadata 和 content 都通过 `Asset → ToolRun → Task` 校验 Actor；另一 Actor 对真实 Asset 得到 404。
- metadata 返回安全状态、尺寸、位深、大小、SHA-256、时间和来源 ID；不返回内部存储定位。
- inline/attachment 都返回 `Content-Type: image/png`、正确 `Content-Length` 和不可注入的 `Content-Disposition`。
- `PENDING` / `FAILED` / `ORPHANED` content 返回 409；非法 disposition 返回 422；MinIO 停止时返回 503 而不是 200。

## 自动化验证

- 最终复审新增部分成功专项：`2 passed in 1.07s`；bounded GET 参数专项：`6 passed in 0.59s`；合并专项：`14 passed in 1.06s`。
- 项目负责人前一轮 6 项 Important 审查专项：`28 passed in 3.34s`；测试先行首次合计 33 个失败实例。
- 阶段计划 M6 聚焦命令：`77 passed in 7.00s`。
- M4/M5 回归：`154 passed in 12.36s`；Tool API：`7 passed in 2.95s`。
- 完整 Backend 新鲜重跑：`563 passed in 39.98s`，0 failed、0 skipped。
- 独立 Mock Runtime：`11 passed in 1.19s`；Backend 与 Mock Runtime 两个测试根合计 574 个不重复测试。
- StorageService + 真实 MinIO：`49 passed in 1.21s`。
- ToolExecutionService unit：`16 passed in 0.08s`；内部回执测试确认同一输出对象且 Adapter 调用 1 次。
- Asset 生命周期故障注入：`24 passed in 0.62s`；Asset API：`8 passed in 4.38s`。
- `python -m pip check`：`No broken requirements found.`
- `python -m compileall -q backend/src mock-runtime/src`：退出码 0。
- Alembic `heads` / `current` 均为 `0006_asset (head)`；`check` 为 `No new upgrade operations detected.`；完整 Backend 自动化验证覆盖安全 downgrade/upgrade 和 ORM 一致。
- `check-scope.ps1 -Milestone M6`：`SCOPE_OK M6`。
- `check-sem-integrity.ps1`：`SEM_INTEGRITY_OK`，57 files，`total_size_bytes=2043071133`，fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- tracked `git diff --check` 通过；13 个 untracked 文件完成等价 trailing whitespace、冲突标记、UTF-8 和最终换行检查；本地 Secret 值扫描通过。

## 真实进程人工验收

使用一次性数据库 `materialsagent_m6_acceptance_20260722a`，独立隐藏启动 Mock Runtime、owner Backend 和 other-Actor Backend，使用真实 PostgreSQL、MinIO 和 HTTP Adapter；结果如下：

| 验收项 | 实际结果 |
|---|---|
| 公共完整合法 Tool 消息 | 503 `TOOL_UNAVAILABLE`；无 result_summary / explanation；selected references 均为空 |
| 受控开发执行 | POST 201；ToolRun `tool_run_712903a3049e4e57b1e90e520c10d252` 保持 `RUNNING` |
| Runtime execute 调用数 | 精确 1 次 |
| Asset | `asset_805558099d334974aef54ada1caa9ef8`，`AVAILABLE` |
| PNG | SHA-256 `f7ab771d76dd6eea15b9df201b81c7286624c831bacf71aa1a4fd55894bfa269`；1480 bytes；PNG/L/512×512 |
| MinIO HEAD | 对象存在，SHA-256、大小和 asset-id 元数据与 PostgreSQL 一致 |
| metadata / inline / attachment | 200 / 200 / 200；Content-Type、Length、Disposition 正确 |
| other Actor | 404 |
| PENDING content | 409 |
| 停止 MinIO 后读取 | 503，未返回假 PNG |
| PostgreSQL 来源一致性 | Task、ToolRun、Asset 的 task/actor/tool_run 关系一致；Task `FAILED/TOOL_UNAVAILABLE`，ToolRun `RUNNING`，Asset `AVAILABLE` |

验收对象、一次性数据库和临时日志均已删除；三个临时进程已停止。最后执行 `docker compose down`，未使用 `-v`；`materialsagent_postgresql_data` 与 `materialsagent_minio_data` 两个命名卷均保留。

## 审查修订补充验收

- 真实 MinIO 正式 PNG 探针：HEAD 的 SHA/大小/MIME/6 项来源元数据精确匹配；919-byte PNG 按 `max_bytes=919` 成功，Adapter 实际读取上限为 920 bytes，`max_bytes=918` 被 `StorageIntegrityError` 拒绝；探针对象已删除。
- 真实停止 MinIO 后 Adapter 返回安全 `StorageUnavailableError: Object storage unavailable.`；MinIO 已重新启动并恢复 healthy，未删除卷。
- 使用真实 PostgreSQL 的 API 测试确认：HEAD 大小不匹配时 GET 调用数 0 且 Asset 转 ORPHANED；AVAILABLE storage unavailable 返回 503 且状态保持 AVAILABLE；AssetService 缺失时 Runtime/ToolRun/Asset 均为 0；无 StorageService 时 ToolRun 查询仍返回数据库中的 asset_ids。
- 所有权故障注入确认：缺失/外部 asset-id、operation-id 或 producer-tool-run-id 均 ORPHANED 且 delete 调用数 0；仅完整三项所有权一致而对象内容不匹配时允许受控 delete。

## M6 精确 allowlist

- `scripts/dev/check-scope.ps1`
- `backend/pyproject.toml`
- `backend/alembic/env.py`
- `backend/alembic/versions/0006_create_asset.py`
- `backend/src/materialsagent/main.py`
- `backend/src/materialsagent/api/dependencies.py`
- `backend/src/materialsagent/api/routes/assets.py`
- `backend/src/materialsagent/api/routes/tools.py`
- `backend/src/materialsagent/application/asset_service.py`
- `backend/src/materialsagent/application/image_payload.py`
- `backend/src/materialsagent/application/png_encoder.py`
- `backend/src/materialsagent/application/tool_execution.py`
- `backend/src/materialsagent/domain/models/asset.py`
- `backend/src/materialsagent/domain/ports/storage.py`
- `backend/src/materialsagent/domain/ports/tool_execution.py`
- `backend/src/materialsagent/domain/ports/unit_of_work.py`
- `backend/src/materialsagent/infrastructure/db/asset.py`
- `backend/src/materialsagent/infrastructure/db/unit_of_work.py`
- `backend/src/materialsagent/infrastructure/storage/minio.py`
- `backend/tests/unit/test_asset_domain.py`
- `backend/tests/unit/test_image_payload.py`
- `backend/tests/unit/test_png_encoder.py`
- `backend/tests/unit/test_storage_contract.py`
- `backend/tests/unit/test_tool_execution_service.py`
- `backend/tests/integration/db/test_asset.py`
- `backend/tests/integration/db/test_chat_orchestration_persistence.py`
- `backend/tests/integration/db/test_migrations.py`
- `backend/tests/integration/storage/test_asset_lifecycle.py`
- `backend/tests/integration/storage/test_minio_storage.py`
- `backend/tests/api/conftest.py`
- `backend/tests/api/test_assets.py`
- `backend/tests/api/test_message_orchestration.py`
- `backend/tests/api/test_tools.py`
- `docs/progress/phase-1-current-status.md`（全局动态状态路径）

## 实际修改文件

实际修改 33 个文件：上述 allowlist 中除 `backend/tests/api/conftest.py` 外的全部路径。该未修改测试文件仍在 allowlist 中，因为 M6 实施前预留了可能需要的 API harness 范围；`domain/ports/tool_execution.py` 是本次负责人明确要求强化安全图片摘要后加入的精确 M6 路径。

没有修改五份设计基线、历史迁移、`SEM/`、`.env`、Mock Runtime 实现或 M7 文件。

## 已知风险与未实现能力

- 当前真实进程验收使用确定性 Mock Runtime，不是真实 ZTA35G 模型；真实模型、权重和 GPU 兼容性仍属于后续已规划里程碑。
- M6 只形成 Asset；ToolResult、Explanation、Task 终态聚合和 selected references 属于 M7，当前明确没有开始。
- 内部 `recover()` 已实现确定 object key 的受控恢复，但本轮没有公共恢复端点，也没有后台 orphan 扫描器；公共重试属于 M8。
- 应用进程不负责启动或守护 PostgreSQL、MinIO、Runtime；依赖不可用时只返回受控错误。
- PostgreSQL 与 MinIO 不能形成单一 ACID 事务，因此 stale PENDING/ORPHANED 是刻意保留的可审计事实，不应被清理脚本静默抹除。

## 下一步

停止在 `READY_FOR_M7`。等待项目负责人明确确认正式进入 M7；本轮不 push、不 amend，也不开始 M7。
