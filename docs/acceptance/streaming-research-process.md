# 研究过程流式展示验收

日期：2026-10-08。范围：当前工作区；修复尚未提交 Git。本次只读核查用户业务数据库已处于 `0025_agent_process_stream`，验收写入使用独立测试数据库。

## 已实现

提交后由 Backend 托管执行，SSE 只负责观察。模型思考、行动说明、工具状态及公开结果保存在过程内容快照中；正式回答仍经原业务事务提交。页面断线/刷新不取消任务，重连读取累计快照。后端重启后显示中断，由用户点击继续；原调用无可验证回执时不重复执行。

前端采用 remend、markdown-it 和 KaTeX：边生成边展示 Markdown，所属消息完成后排版公式。过程可折叠、历史可回看，已中断的片段标为未完成。未增加科学结论 Validator。

## 原始验收证据

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

## 修复后复验（2026-10-08）

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

- 新迁移为 `0025_agent_process_stream`。本次只迁移临时测试数据库，不清理用户历史；用户业务库的当前版本已只读核查为 `0025_agent_process_stream`。部署新代码仍需重启 Backend/前端。
- 第一版依赖单个 Backend 进程的任务托管和订阅广播；多 worker/分布式执行不在本次范围。
- 历史保存累计内容，不保存逐 token 的播放轨迹。硬断电可能丢失最后约 1 秒尚未持久化的片段。
- 后端重启、晚到回执和不重复工具派发由数据库/SDK 回归覆盖；未在浏览器中强杀后台进程。真实 EBSD GPU 与独立 ML 浏览器链路的补跑结果见上表，未覆盖真实 Provider 驱动的完整 ML 流程和长时 SEM 生成。
- build 有约 501kB 主 JS chunk 的体积提示。`npm audit` 的 9 项告警来自原有 Vue/Vitest/构建及测试依赖链，本次新增 Markdown/数学依赖未出现在告警列表；未执行无关的全量依赖升级。

测试过程的日志、浏览器截图和辅助脚本保存在忽略目录 `tmp/`，不属于生产代码或提交内容。架构与协议见 [流式展示设计](../design/streaming-research-process.md)。
