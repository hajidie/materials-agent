# P1B2 门四真实 Runtime 与综合验收 Runbook

## 1. 用途与边界

本 Runbook 仅用于 P1B2 门四。执行入口为：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 `
  -Action <MockHelperTests|MockFullRegression|MockRegression|StageB|Phase1|Phase2|Phase3|Audit|Cleanup>
```

当前安全整改检查点只允许 `-SelfTest`、`-Action MockHelperTests` 和完整受控
`-Action MockFullRegression`；兼容名 `MockRegression` 固定映射到完整回归。
第五次
Stage B 固定拒绝为 `P1B2_GATE4_STAGE_B_FINAL_ATTEMPT_BLOCKED`；新的 Stage C
Phase1 固定拒绝为 `P1B2_GATE4_NEW_STAGE_C_NOT_AUTHORIZED`；Phase2 及后续动作
继续 fail closed。下述真实规程只作为未来重新授权后的流程定义，当前不得执行，
也不得输出浏览器 READY。

`-SelfTest` 只执行离线检查；不得启动 Docker、Runtime、CUDA、Frontend、
Backend 或连接 Provider。

当前正式状态：

```text
P1B2 门四 Stage B:
COMPLETE / PROJECT_OWNER_ACCEPTED

P1B2 门四 Stage C:
BLOCKED / SAFETY_REMEDIATION_REQUIRED

当前 Provider 预算:
UNAUDITABLE / CURRENT_RUN_INVALIDATED

新的真实 Stage C:
NOT AUTHORIZED
```

当前作废 run 已明确观察计划外真实 Chat delegate `>= 8`，精确总数不可恢复；
旧 Runner 的 Provider `0/5` 不得作为事实。事故标记为
`P1B2_GATE4_PROVIDER_BUDGET_INCIDENT_RECORDED`。Stage B 已接受的 Runtime
`1/2`、DDPM `1/2` 证据继续有效。

Runner 只允许修改本轮临时资源和 `tmp/phase-1b-gate4/` 下由本轮创建的产物。
不得修改 `.env`、`SEM/`、正式数据库、正式 MinIO bucket、命名 volume 或生产代码。
不得把任何凭据放入命令行、日志、state、summary、JUnit、JSON 或报告。

真实调用必须使用同一 `run_id` 和预算账本。预算上限为：

```text
Chat Provider delegate       3
Explanation Provider delegate 2
Provider delegate total      5
Runtime execute delegate     2
DDPM sampling                2
```

达到预算后，下一次调用必须在 delegate 前分别以
`P1B2_GATE4_PROVIDER_BUDGET_EXCEEDED` 或
`P1B2_GATE4_RUNTIME_BUDGET_EXCEEDED` 拒绝。

## 2. 执行前检查

执行任何真实动作前，Runner 必须确认：

- 当前 branch、HEAD、subject、parent 与授权恢复基线一致；
- 暂存区为空；
- 工作区精确为 4 个 tracked modified、4 个授权 untracked、unexpected 0，
  没有 rename 或 staged path；当前实施阶段不要求 `Untracked=0`；
- `.env` 存在，并记录开始 SHA-256；
- 以受控 `KEY=VALUE` 解析读取所需配置，不执行 `.env` 内容；
- `DEEPSEEK_API_KEY` 存在、非空、无首尾空白、无控制字符；
- 3000、8000、8100 端口及 GPU 状态满足当前阶段的资源门；
- 既有 state 与本轮 repo、HEAD、run_id 和阶段一致。

`.env` 只在父 Runner 内存中读取。真实 Provider Key 只能进入使用真实 Provider
的 Backend 子进程环境；不得进入 Runtime、Frontend、Docker Compose、
PostgreSQL、MinIO 或日志收集进程。故意无效 Key 必须随机生成、与真实 Key
无关且不相等，也只能进入场景 5 的 Backend 子进程环境。

若 Key 不可用，在启动任何服务前输出：

```text
P1B2_GATE4_DEEPSEEK_KEY_NOT_AVAILABLE
```

## 3. 离线 SelfTest

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 -SelfTest
```

期望仅输出安全的固定标记：

```text
P1B2_GATE4_EXECUTOR_SELF_TEST_OK
P1B2_GATE4_POWERSHELL_SELF_TEST_OK
```

SelfTest 必须使用临时假值和 fake child，覆盖 `.env` 解析、Key 缺失 fail
closed、角色环境隔离、Key 精确扫描、双预算、双 Backend 重启、同一临时资源
复用、环境恢复、幂等 cleanup，以及失败时绝不写 `PASSED`。

纯离线安全 helper 使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 `
  -Action MockHelperTests
```

最终完整 Mock 回归必须使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 `
  -Action MockFullRegression
```

完整入口复用既有 `run-phase-1a.ps1`，在系统临时目录从固定 HEAD 创建只含已
跟踪文件的干净快照；不得修改真实 index/working tree，不得复制或链接根
`.env`。临时 `.env` 只写入白名单 Phase 1A 基础设施字段，并强制 Mock adapter、
空 Provider Key 和空全部真实授权；退出后必须删除快照及本轮 owned Mock 资源。
成功必须输出 `P1B2_GATE4_MOCK_FULL_REGRESSION_OK`；helper 的
`P1B2_GATE4_MOCK_HELPER_TESTS_OK` 不能替代完整回归成功。
发送 Mock stack start 命令前即承担 cleanup responsibility；start 非零、超时或
缺 marker 也必须调用幂等 stop，并核对 3000/8000/8100、
5432/9000/9001 以及固定 `materialsagent` Compose 容器集合。启动前若这些目标
已有 listener/container，必须 fail closed，不得停止或复用未知资源。

受控 Runner 为每个 Mock 子进程显式设置 `LLM_ADAPTER=mock`，以空白值覆盖
`DEEPSEEK_API_KEY`，移除 M12-B/P1B2 全部真实调用授权。test collection 前在
同一精确环境验证 `AppSettings().llm_adapter == "mock"`、DeepSeek Chat 与
Explanation Adapter 构造次数 0、Provider delegate 次数 0；任一失败固定为
`P1B2_GATE4_MOCK_PROVIDER_ISOLATION_FAILED`。门四期间禁止直接手工运行
`python -m pytest backend/tests`。

## 3.1 未来 fresh Stage C 的权威 Provider 账本

- 该账本是 Provider delegate 边界的权威保守计数，不是无条件等于 Provider
  实际收到的网络请求数。正常、无进程崩溃时应与当前 run 的 DeepSeek
  `LLMCall` 数据库事实一致。
- Provider 账本与 Runtime/DDPM 账本分离，只保存 schema version、run id、
  Chat/Explanation/total attempts、maximum attempts、state 和 updated time。
- `state` 只允许 `EXACT`、`UNKNOWN`、`INVALIDATED`，不得保存 Prompt、正文、
  回复、Key、Token、Base64 或异常正文。
- 每次真实 Adapter delegate 前获取锁，读取并验证 run/budget/计数单调性；先把
  attempt 增加写入临时文件、fsync、原子替换正式账本、重新读取确认，并与当前
  database 的 `LLMCall` purpose 计数核对，然后才允许 delegate。
- 真实 Key、无效 Key、恢复真实 Key 的三次 Backend 都必须通过
  `create_app()` 显式端口注入共享同一个账本。第六次调用在 delegate 前拒绝。
- 缺失、损坏、run id 不匹配、字段缺失、计数回退、数据库冲突、写入或读回失败
  均固定 `P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN` 并停止真实验收。
- attempt 预留后、Adapter 调用前若进程异常退出，账本可以保守多计，但不会
  少计或允许超预算调用；该 run 必须为 `UNKNOWN` 或 `INVALIDATED`，不得由新
  Backend 恢复成可继续的 `EXACT`，也不得写 `PASSED`。

## 3.2 进程 ownership 与 Phase1 原子性

进程身份同时验证 role/port、PID、start time、完整 executable path SHA-256、
`command_argument_count`、`command_arguments_sha256`、marker 和监听 port
owner。参数摘要严格保持 `argv[1:]` 顺序与每个完整 token，以 UTF-8 紧凑 JSON
数组规范化后计算 SHA-256，不保存完整参数。marker 仍存在但脚本/action 错误，
或参数发生增删/重排时，必须以 `failure_field=command_arguments` 拒绝。role
由 record schema、精确 argv 和唯一 marker 共同约束；port 独立证据为实际
LISTENING 地址与 owner PID。路径含空格和引号参数必须正常；退出、PID 重用或
任一字段不匹配必须拒绝。诊断只允许
`role/pid/expected/actual/failure_field`。

Phase1 的 store 准备、database/bucket 创建、Backend/Frontend 启动、健康检查
或 ownership 任一步失败，都必须停止本轮 owned processes、删除本轮精确
database/bucket、释放端口、恢复环境并把 run 标为 INVALIDATED。未来重试必须
在新的项目负责人授权下使用不同 run id 和全部新的临时资源。

## 4. Stage B：真实 Runtime 直连

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 -Action StageB
```

Stage B 使用：

```text
URL: http://127.0.0.1:8100
bundle: zta35g-sem-original-bundle
device: cuda
外层 watchdog: 1200 秒
主机空闲内存硬门: 5905580032 bytes（5.5 GiB）
```

该门必须在设置真实模型授权、启动 Runtime 或打开权重之前，以
`GlobalMemoryStatusEx.ullAvailPhys` 的实际字节值判断；少 1 byte 即固定失败为
`P1B2_GATE4_STAGE_B_MEMORY_PREFLIGHT_FAILED`，真实预算保持 0。

Runner 必须完成 live/ready/execute 的 Token 负测、非法请求零 delegate、busy
立即 `503`、固定 combined execute、Base64 NPY 安全解码、有限正数性能结果、
全程资源采样和响应契约审计。真实 execute 前先预留：

```text
Runtime 1/2
DDPM 1/2
```

Stage B 必须把模型加载增量与推理增量分开测量并记录：

```text
stage_b_before_runtime_start_free_bytes
stage_b_after_runtime_ready_free_bytes
stage_b_pre_execute_free_bytes
stage_b_minimum_during_execute_free_bytes
stage_b_post_execute_free_bytes
stage_b_model_load_drop_bytes =
  max(0, before_runtime_start - after_runtime_ready)
stage_b_execute_drop_bytes =
  max(0, pre_execute - minimum_during_execute)
stage_b_total_drop_bytes =
  max(0, before_runtime_start - minimum_during_execute)
```

Runtime ready 后且尚未 execute 时至少等待 2 秒；after-ready 和 pre-execute
各取 3 次、间隔 250 ms 的中位数。Stage C 只使用本轮
`stage_b_model_load_drop_bytes` 与 `stage_b_execute_drop_bytes`，不得使用
`stage_b_total_drop_bytes` 或门三历史总下降量代替。

低于 `2147483648 bytes`（2.0 GiB）记录 `HOST_MEMORY_LOW_WARNING` 的首次时间、
最低值和持续时间，但不终止当前 DDPM；低于 `1610612736 bytes`（1.5 GiB）
记录 `HOST_MEMORY_CRITICAL_LOW`。调用前命中 critical
不得 delegate；调用中命中则等待当前调用返回后立即停止 Runtime、禁止后续场景
并清理；不得 CPU fallback、降低尺寸/timesteps 或启用 AMP/半精度。

无论成功或失败都必须按已验证的本轮 ownership 停止完整 Runtime 进程树，确认
8100 无监听、GPU 恢复基线，复核 `.env` SHA-256，并扫描或隔离污染产物。只有
完整清理成功才能进入综合栈。

## 5. Phase 1：综合栈与浏览器场景 1–3

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 -Action Phase1
```

Runner 创建本轮唯一的临时 PostgreSQL database、MinIO bucket、Actor、
Conversation、`run_id` 和预算账本。Backend 使用真实 Key，Frontend 启动，
真实 Runtime 故意保持停止。Backend 固定：

```text
LLM_ADAPTER=deepseek
ZTA35G_RUNTIME_URL=http://127.0.0.1:8100
ZTA35G_RUNTIME_TIMEOUT_SECONDS=900
urllib3.Timeout(total=900)
retries=False
```

到达暂停点后 Runner 输出：

```text
P1B2_GATE4_BROWSER_ACCEPTANCE_READY
Frontend:
http://127.0.0.1:3000
真实 Runtime：
当前故意未启动
请完成场景1–3并回复结果。
```

项目负责人在同一 Conversation 中完成人工场景：

1. Knowledge：Task `SUCCEEDED`，`task_type=KNOWLEDGE_QA`，无 ToolRun；
2. NEEDS_INPUT：Task `NEEDS_INPUT`，显示 `missing_fields`，无 ToolRun；
3. Runtime unavailable：稳定 Tool Task 和 ToolRun attempt 1 `FAILED`，无
   Result/Asset，Runtime delegate 仍为 1。

此时账本必须为：

```text
Chat=3
Explanation=0
Provider total=3
Runtime=1
DDPM=1
```

收到项目负责人明确确认前不得继续 Phase 2。

## 6. Phase 2：无效 Key 下 Tool retry

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 -Action Phase2
```

浏览器、Frontend、Backend、PostgreSQL 和 MinIO 均已实际运行、真实 Runtime
尚未启动时，Runner 先测量 `stage_c_before_runtime_start_free_bytes`：

```text
required_stage_c_runtime_start_free_bytes =
max(4831838208, stage_b_model_load_drop_bytes + 1610612736)
```

只有实时读数达到要求才允许启动 Runtime 和加载模型；否则固定失败为
`P1B2_GATE4_STAGE_C_RUNTIME_START_MEMORY_FAILED`，不得打开权重或增加预算。

Runner 随后安全停止初始 Backend、确认
8000 端口释放，以内存中的故意无效 Key 启动新 Backend。数据库、bucket、
Actor、Conversation、Timeline Key、Runtime URL/Token 和 timeout 必须不变；
父进程真实 Key 不得被覆盖。

真实 Runtime ready 且模型已加载后至少等待 2 秒；在允许项目负责人点击“重试
Tool”之前，Runner 必须测量 `stage_c_pre_tool_retry_free_bytes`，并计算：

```text
required_stage_c_tool_retry_free_bytes =
max(3758096384, stage_b_execute_drop_bytes + 1610612736)
```

只有实测值达到动态要求才输出
`P1B2_GATE4_TOOL_RETRY_RESOURCE_READY`。未输出该标记时禁止浏览器写请求；
失败固定为 `P1B2_GATE4_STAGE_C_TOOL_RETRY_MEMORY_FAILED`，不得执行第二次
Runtime/DDPM，也不得增加 Provider delegate，随后安全清理并保留 Stage B 证据。

Stage B 门覆盖模型加载、常驻模型和第一次推理；Stage C Runtime 启动门只覆盖
综合栈实时状态下的模型加载增量；Stage C Tool retry 门只覆盖模型已加载后的
单次推理增量。浏览器及综合栈占用已经包含在 Stage C 实时空闲内存读数中，不得
把 Stage B 总下降量再次加到 Tool retry 门。

统一 8 GiB、6.75 GiB、五次采样中位数 6.70 GiB 以及 Stage B 6.5 GiB /
Stage C 6.25 GiB 方案均为已废止历史规则，不得作为当前并行判断。

本资源门是针对当前约 15.4 GiB 物理内存主机的验收配置，目标是在浏览器和综合
栈正常运行时尽量完成真实模型验收；它不是生产部署推荐配置，也不保证其他硬件
具有相同性能。

项目负责人点击同一失败 Task 卡的“重试 Tool”。预期：

- ToolRun attempt 2 使用不同 seed 并成功；
- Runtime 与 DDPM 各增加一次，均达到 2/2；
- Result、AVAILABLE Asset、正式 PNG 与 ResultAssetLink 成功；
- 自动 Explanation 真实 delegate 一次并因无效凭据认证失败；
- 失败使用既有安全错误分类，不自动重试，不暴露 Provider 原始响应；
- Task 为 `PARTIALLY_SUCCEEDED`，结果和图片保留并显示“重试解释”；
- selected ToolRun/Result 指向 attempt 2，attempt 1 历史保留。

若无效 Key 导致 Backend 启动失败而不能到达 Provider 调用，立即输出
`P1B2_GATE4_INVALID_KEY_SCENARIO_NOT_REACHABLE`，不得追加调用。

场景 6 同时核对 PNG 下载的 `Content-Type=image/png`、PNG magic、512×512、
对象与公共下载 SHA-256 一致，且公共响应无 object key、bucket 或内部路径。

## 7. Phase 3：恢复真实 Key并重试 Explanation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 -Action Phase3
```

Runner 停止无效 Key Backend，确认 8000 释放，用一直保存在父进程内存中的真实
Key 启动第三个 Backend。不得重启 Runtime，不得创建新 database、bucket、
Actor 或 Conversation。

项目负责人点击“重试解释”。预期 Explanation attempt 2 成功，Task 从
`PARTIALLY_SUCCEEDED` 变为 `SUCCEEDED`；Result、Asset、ToolRun 和 selected
references 不变；Runtime/DDPM 不增加。最终账本：

```text
Chat=3
Explanation=2
Provider total=5
Runtime=2
DDPM=2
```

刷新并重新打开 Conversation 后，两个 ToolRun attempt 和两个 Explanation
attempt 均可查看，最新成功 Explanation 正确，Console warning/error 为 0，
页面无横向溢出。

## 8. Audit 与 Cleanup

`Audit` 只读核对 Actor、Conversation、Task、ToolRun、Asset producer、
ToolResult、ResultAssetLink、Explanation、LLMCall、selected references、
Provider/Runtime/DDPM 账本和 MinIO object。必须只有一个本轮 PNG object，
不得有 direct-runtime NPY object。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 -Action Audit
```

清理顺序固定为：停止浏览器写操作、Frontend、Backend，确认 Runtime 无执行，
停止 Runtime，删除本轮 object/bucket，终止临时数据库连接并删除 database，
只停止本轮启动的 Compose 服务，最后恢复父进程环境变量。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/acceptance/run-phase-1b.ps1 -Action Cleanup
```

必须确认 3000/8000/8100 无监听、GPU 恢复基础状态、临时 database/bucket
不存在、正式资源和命名 volume 不变、`.env` SHA-256 未变，并以内存中全部
敏感值精确扫描所有产物。发现泄漏时隔离并删除污染产物，只保留：

```text
P1B2_GATE4_SECRET_LEAK_DETECTED
```

任何清理不完整时输出：

```text
P1B2_GATE4_CLEANUP_INCOMPLETE
```

此时不得写整体 `PASSED`。

## 9. 最终状态

只有浏览器场景、数据库/对象/预算审计、清理和完整 Mock 回归全部通过，才可在
文档中写：

```text
P1B2_GATE4_COMPLETE_AWAITING_PROJECT_OWNER_REVIEW
```

场景 5 必须表述为“Tool 成功、自动 Explanation 使用故意无效凭据发生一次受控
真实 Provider 认证失败，因此 Task=`PARTIALLY_SUCCEEDED`”；场景 7 才是恢复
真实 Key 后显式 Explanation retry 成功，最终 Task=`SUCCEEDED`。不得写
`PROJECT_OWNER_ACCEPTED`，不得记录任何真实或无效 Key 值。
