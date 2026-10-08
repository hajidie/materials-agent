# 研究过程流式展示验收

日期：2026-10-08。流式实现已提交于 `74cedf4`，审查修复已提交于 `0bf0db5`。下文保留实施与修复时的验收证据；当时只读核查用户业务数据库处于 `0025_agent_process_stream`，验收写入使用独立测试数据库。本页运行态核查属于该阶段的历史快照；后续容错改动、`0026_managed_outcome_unknown` 迁移及最近现场核查见 [容错验收](reliability.md)，不能由历史通过结果推导当前状态。

## 已实现

提交后由 Backend 托管执行，SSE 只负责观察。模型思考、行动说明、工具状态及公开结果保存在过程内容快照中；正式回答仍经原业务事务提交。页面断线/刷新不取消任务，重连读取累计快照。后端重启后显示中断，由用户点击继续；原调用无可验证回执时不重复执行。

前端采用 remend、markdown-it 和 KaTeX：边生成边展示 Markdown，所属消息完成后排版公式。过程可折叠、历史可回看，已中断的片段标为未完成。未增加科学结论 Validator。

## 原始验收证据（历史记录）

| 验证 | 结果与范围 |
| --- | --- |
| Backend 全量 | Python 3.11 环境 `materialsagent-backend`，`python -m pytest backend/tests -q --disable-warnings --maxfail=5`：981 passed，255.45s；包含真实 PostgreSQL/MinIO，未加载 GPU 权重。 |
| 补充边界 | 后续增加调度交接回归并加强过程级联删除断言；`test_agent_process_stream.py` 与 `test_agent_process.py` 合计 8 passed。 |
| 增量迁移 | `test_process_migration_preserves_existing_runs_and_guards_downgrade`：1 passed。已有 Run/消息保留；存在 INTERRUPTED 时拒绝降级，解决中断后可降级。 |
| Vue | Vitest 全量 88 passed；`npm --prefix frontend run typecheck` 与 `npm --prefix frontend run build` 通过。 |
| 浏览器 | Chromium 经 Vite 18762 → FastAPI 18761 → 独立临时 PostgreSQL；使用延迟分片测试模型和真实单位换算工具，观察到完成前思考/正文、刷新续看、过程默认折叠/手动展开、表格/代码/公式、尾部字面符号、390px 窄屏。 |
| 网络与操作 | 浏览器离线后重新联网恢复最终内容；刷新中途任务没有重复派发。点击中止后显示停止且输入恢复。向上滚动至顶部后，后续分片到达仍保持 scrollTop=0。 |
| 真实 DeepSeek | 当前配置的 `deepseek-flash` thinking/high：直接数值换算请求成功，2 次模型调用、1 次工具执行，7.3s；完成前已读取公开过程，保存 132 字符思考内容。不是 GPU/科研精度验收。 |

真实 Provider 的首个请求要求 Markdown 表格和公式，模型同时提交了不合法的单位声明，原 `INVALID_UNIT_ASSERTION` 校验将其终止；过程仍可保存（778 字符思考），未绕过校验。明确“直接数值换算、不涉及数据列声明”的第二个请求完成上述成功链路。两例只用于流式与工具续轮验收，不能推导通用参数生成正确率。

## 修复后复验（2026-10-08，历史记录）

PostgreSQL 恢复可用后，在确认重放、回复草稿和 ML 验收脚本修复后的工作区补跑。数据库与存储验收使用生成的独立测试数据库和存储桶，由 fixture 清理；本次未迁移用户业务数据库，只读核查其版本已为 `0025_agent_process_stream`。

| 验证 | 结果与范围 |
| --- | --- |
| 数据库 API 与集成 | `test_agent_turns.py`、`test_agent_process_stream.py`、`test_agent_sdk_recovery.py`、`test_agent_control.py`：55 passed；其余 `backend/tests/api` 与全部 `backend/tests/integration`：267 passed。合计 322 passed，覆盖确认重放不重复派发、SSE、重启恢复、迁移、资源及真实 MinIO。 |
| ML Service/MCP 自动验收 | 独立 ML Python 3.11，`ML_INTEGRATION=1`、`P4_BACKEND_PYTHON` 指向隔离 Backend 环境；`tests/service -k 'not opt_in and not real_browser'` 串行运行：123 passed，870.14s。该筛选也排除了一个普通权限合同，单独补跑 `test_three_credential_roles_are_distinct_and_mcp_is_opt_in`：1 passed，合计 124 passed。包含 LR/RF 训练/预测、10 项平台 MCP 用例、超时/Backend 死亡恢复、不重复执行、Worker 进程树、资源回执与存储发布；真实 Provider 的两项 ML 用例未运行，浏览器单列。 |
| 真实 DeepSeek 补跑 | `deepseek-flash`：SUCCEEDED，2 次模型调用、1 次单位换算工具调用，8.1s；完成前已有流式内容，保存 554 字符思考。第一次补跑在 100s 观察窗口结束时仍 RUNNING，不计通过；延长观察窗口后的独立请求完成。 |
| 真实浏览器 ML | Edge，构建产物 → FastAPI → MCP/ML Service → Worker → 独立 PostgreSQL/MinIO；`test_real_browser_resource_workflow`：1 passed。使用 scripted 测试模型，实际上传 60 行 CSV、确认 LR 训练、预测 10 行并下载两份 CSV；检查只读详情保留草稿、Enter 提交、Escape 返回焦点、刷新后结果通知仅一条、390px 窄屏及断网后重新加载详情。 |
| 真实 EBSD GPU HTTP | Python 3.8 `test_ebsd_real_gpu_independent_http_and_sem_isolation`：1 passed，28.27s。5 张样本预测、原始 CNN 输出对照、鉴权/输入错误、忙碌与异常处理及模型状态隔离均通过。未开启约 20 分钟的双轮 SEM 生成分支；该测试独立于 Backend/数据库。 |
| 环境隔离 | Backend 与独立 ML Python 均为 3.11；各自 `pip check` 通过，Backend 未安装 ML Engine/sklearn/joblib。真实模型仍只在 Python 3.8 Runtime 加载。 |

本次浏览器验收的常驻 Worker 与并行 ML 测试曾争用 `local-training-worker-v1` 本机单实例锁，出现 `WORKER_ALREADY_RUNNING` / `ML_WORKER_STOPPED`。这些受干扰的结果不计入通过；浏览器 Worker 退出后串行重跑上述自动验收全部通过。后续 ML 自动验收必须与浏览器 Worker 串行运行。日志、JUnit XML、CSV 和截图保存在忽略目录 `tmp/`。

## 部署与限制

- 流式复验当时 Backend 迁移 head 为 `0025_agent_process_stream`，后续迁移见 [容错验收](reliability.md)。上述复验只迁移临时测试数据库，不清理用户历史；用户业务库的版本是复验当时的只读核查结果。`scripts/dev/local-dev.ps1 -Action Start` 会升级所配置的 Backend 数据库、初始化 SDK checkpoint 表并启动服务；已有服务需重启才能加载新代码。
- 第一版依赖单个 Backend 进程的任务托管和订阅广播；多 worker/分布式执行不在本次范围。
- 历史保存累计内容，不保存逐 token 的播放轨迹。保存由内容更新和收口触发，不是固定定时器；硬断电可能丢失自上次保存以来的片段，约 1 秒的保存节流不构成丢失上界。
- 后端重启、晚到回执和不重复工具派发由数据库/SDK 回归覆盖；未在浏览器中强杀后台进程。真实 EBSD GPU 与独立 ML 浏览器链路的补跑结果见上表，未覆盖真实 Provider 驱动的完整 ML 流程和长时 SEM 生成。
- 前端依赖安全告警已通过兼容升级修复；主 JS 体积提醒仍保留，升级与复验结果见下文“依赖与构建告警复核”。

测试过程的日志、浏览器截图和辅助脚本保存在忽略目录 `tmp/`，不属于生产代码或提交内容。架构与协议见 [流式展示设计](../design/streaming-research-process.md)。

## 流式阶段代码与运行态核查（2026-10-08 历史快照）

- 核查基线为 `main` 的 `0bf0db5`；收尾开始时工作树干净。Backend Python 3.11.15 下只读运行 `alembic -c backend/alembic.ini heads`，返回 `0025_agent_process_stream (head)`；这只证明代码迁移链，不证明业务库版本。
- 本次运行 `test_agent_process.py`、`test_sdk_agent_loop.py`、`test_agent_store_confirmation.py`：12 passed。前端 `assistant-markdown.test.ts`、`process-streams.test.ts`、`app-flow.test.ts`：21 passed。这些是离线合同与 jsdom 回归。
- 文档收尾开始时本地栈未启动；用户随后启动平台，于 2026-10-08 补做运行态验证。Backend live/ready 返回 200，PostgreSQL 与对象存储均为 `AVAILABLE`，ML Service ready 返回 200，前端默认地址返回 200。启动记录为 Real Runtime / Provider 模式。
- `/api/v1/tools` 的 7 个启用工具全部为 `AVAILABLE`：单位换算、ZTA35G、EBSD，以及 ML 分析、训练提交、训练查询和预测。此项证明就绪探针通过，实际执行范围见下一项。
- 浏览器提交独立验证消息，将 25 °C 换算为 K：Run 为 `SUCCEEDED`，2 次模型调用、1 次单位换算执行，结果 298.15 K。页面经历进行中与完成态，公式正确排版；刷新后回答和研究过程恢复，保存 60 字符模型思考，未出现重复工具执行。完成态 SSE 重连返回 `snapshot`、`settled`，浏览器控制台未捕获 warn/error。
- 只读查询业务库 `alembic_version` 为 `0025_agent_process_stream`。本次未执行迁移、停服或删除验证对话；验证对话保留供复核。
- 该次核查的基础链路为 `verified-current`。未重跑 GPU 推理、SEM 生成、ML 训练/预测完整链路、后台强杀恢复或全量验收；未测量完成前首段内容到达时间。上述场景继续使用历史证据，不由该次就绪和单位换算验证推导通过。

## 依赖与构建告警复核（2026-10-08）

修复前重新运行 npm CLI 审计成功，确认 9 个受影响依赖条目：6 high、3 moderate。计数按包及依赖传播汇总，同一公告可计入多个包。此次保留直接依赖的精确版本锁定，更新 `package-lock.json`，未跨主版本升级或添加强制覆盖：

| 依赖 | 修复前 → 修复后 |
| --- | --- |
| `vue` 及其编译器、运行时、`@vue/server-renderer` | 3.5.40 → 3.5.43；覆盖 [Vue SSR 公告](https://github.com/advisories/GHSA-g2v6-rqmx-r4w6)。 |
| `vitest`、`@vitest/mocker` 及同版本配套包 | 4.1.10 → 4.1.11；覆盖 [Vitest 公告](https://github.com/advisories/GHSA-82fw-gwwq-j7x9)。 |
| `brace-expansion` | 2.1.2 → 2.1.7 |
| `nanoid` | 3.3.16 → 3.3.20 |
| `postcss` | 8.5.22 → 8.5.29 |
| `source-map-js` | 1.2.1 → 1.2.2 |
| `undici` | 7.28.0 → 7.30.0 |

修复后 `npm --prefix frontend audit --json` 成功，退出码为 0，所有等级合计 **0 项已知漏洞**；`npm --prefix frontend ls --all --json` 通过。该结果覆盖当前前端依赖和 npm 公告库，不代表完成整个平台的安全审计。早先核查曾遇到 npm CLI 的 TLS 建连失败，并以官方 bulk advisories 接口响应复核；此次修复前后的 CLI 审计均已成功，不再依赖该历史替代证据。

升级后复验：Vitest 全量 **13 个文件、90 项测试通过**；独立 `typecheck` 与 `build` 通过。主 JS 为 **502.93 kB**，gzip 为 **171.43 kB**（升级前为 501.16 / 170.83 kB）。Vite 按 gzip 前的 chunk 体积比较默认 500 kB 阈值，因此仍发出性能提醒；本次保留默认阈值，将拆包或按需加载留待实际加载性能需要时处理。阈值定义见 [Vite 文档](https://vite.dev/config/build-options.html#build-chunksizewarninglimit)。

运行中的 Vite 曾缓存 Vue 3.5.40；本次仅重启受启动脚本管理的前端，强制重建依赖缓存并更新启动状态记录。缓存已核实为 3.5.43，其余受管进程身份保持有效。Backend ready 与 7 个启用工具的 `AVAILABLE` 探针均通过。真实浏览器请求单位换算得到 **0 °C = 273.15 K**，工具执行成功，公式与 3 项研究过程正常显示；刷新后恢复，控制台未捕获 warn/error。验证对话保留供复核；本轮没有重跑 GPU、SEM 或 ML 训练/预测。

修复前后审计为 `tmp/dependency-audit-before-20261008.json` 与 `tmp/dependency-audit-after-20261008.json`；测试、类型检查和构建日志为 `tmp/dependency-*-20261008.txt`，截图为 `tmp/dependency-browser-detail-20261008.png`。此前运行态记录和截图继续保存在 `tmp/runtime-verification-20261008.json`、`tmp/runtime-browser-20261008.png` 与 `tmp/runtime-browser-detail-20261008.png`。这些本地证据均被忽略，不提交用户数据。
