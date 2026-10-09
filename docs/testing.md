# 验证指南

先用最接近改动的测试验证行为，再根据影响的合同扩大范围。测试文件存在不代表本次已运行，更不代表真实模型或浏览器链路通过。仓库没有统一的覆盖率百分比门槛。

## 常用命令

在仓库根目录运行；Python 命令必须使用对应环境。Backend 与 Mock Runtime 分开执行，因为两侧都有同名的 `test_ebsd.py`。

```powershell
# materialsagent-backend，Python 3.11
python -m pytest backend/tests/unit/test_agent_runtime.py -q
python -m pytest backend/tests -q
python -m pytest mock-runtime/tests -q

# 前端，Node.js 24.14.x / npm 11.9.x
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build

# materialsagent-zta35g，Python 3.8；不加载真实权重
python -m pytest zta35g-runtime/tests/unit zta35g-runtime/tests/contract -q

# 独立 ML Python 3.11
python -m pytest services/materials_ml/tests/test_engine.py -q
```

Backend 的 `api/` 与 `integration/` 测试可能使用真实 PostgreSQL；存储集成测试使用 MinIO。先按 [README](../README.md#本地启动) 准备本地依赖。前端 Vitest 使用 jsdom 和请求替身，不等于浏览器或真实 Backend 验收。

统一脚本 `scripts/acceptance/run-agent-acceptance.ps1` 依次执行 Backend、Mock Runtime、前端及本地脚本检查，要求 Backend Python 3.11 和已就绪的数据库、对象存储；其中的“离线”只表示不调用真实 Provider/GPU。独立 ML 的 Service/Worker/MCP 验收入口和配置见 [ML 服务说明](../services/materials_ml/README.md)。

ML 自动验收需与浏览器 ML 验收、常驻 Worker 串行运行；单实例锁和环境准备见 [ML 验收说明](../services/materials_ml/README.md#独立服务真实验收)。受争用影响的失败需在串行环境复验，不能直接计为通过。

## TC4 批量分割

定向入口为 `backend/tests/api/test_tc4.py`、`mock-runtime/tests/test_tc4.py`、`zta35g-runtime/tests/contract/test_tc4_http.py` 和 `frontend/tests/components/tc4-flow.test.ts`，分别使用上文对应环境运行。Backend 测试覆盖 1/5/10 图有序集合绑定、一次父调用、输入失败继续与全部失败、丢失响应查原回执、保存失败与 Backend 重启恢复、Runtime 丢失回执不重派发、停止后保存迟到结果，以及删除清理 outbox。Python 3.8 合同测试覆盖原身份重放、崩溃遗留未知、共享 GPU 锁、成果读取与删除标记；Mock 结果使用 `mock-tc4-bundle` 版本。

图片合同回归覆盖灰度/RGB、完全不透明的 RGBA PNG、原文件无损读取与 RGB 转换像素一致，以及一个像素透明或半透明时拒绝处理。RGB/灰度 PNG 的 `tRNS` 按实际像素判断；16 位 RGB PNG 即使解码模式为 RGB 也拒绝。Backend、真实 Runtime 与 Mock Runtime 分别验证这些输入边界；测试不依赖或提交用户图片。前端回归覆盖加载超过最近 20 个 Run 的较早消息后，仍显示逐图状态、像素统计和面积占比。

真实验收使用一组适用样图（例如五张），逐张比较原程序与新 Runtime 的类别掩膜、叠加图、原尺寸和像素统计；通过真实 Provider 的浏览器批量上传，核实一次 Agent 工具调用、逐图回执与统计，以及每张输入对应两张成果。另检查刷新、停止、故障暂停与继续，以及与 SEM/EBSD 的 GPU 互斥。比较产物放在忽略的本地工作目录；单次结果记录在 Issue/交付回复，没有人工标注时不报告分割准确率。

## 按风险选择验证

| 改动 | 至少检查 |
| --- | --- |
| 局部 Python 逻辑 | 直接相关的 `test_*.py`；缺陷修复添加回归用例 |
| API、工具合同、状态或迁移 | 相关 unit、contract、API 和真实数据库用例；必要时扩大至 Backend 全量 |
| Vue 组件或交互 | 对应 `*.test.ts`；共享类型运行 typecheck，打包相关改动运行 build |
| 本地启动脚本 | `scripts/dev/test-local-dev.ps1`；覆盖完整启动参数与退出码、真实模式约束、功能开关覆盖、TC4 路径解析和 ML 能力就绪检查；范围检查脚本改动运行 `test-check-scope.ps1` |
| 跨服务 ML 或真实模型 | 对应独立环境的验收；分别记录 Service、MCP、GPU 和浏览器结果 |

最后运行 `git diff --check`，检查实际 diff 与未提交文件，并记录未执行的验证及原因。文档改动至少核对命令、路径和相对链接。

完整栈现场检查使用 `scripts/dev/start-all.ps1`，确认 `/api/v1/tools` 中八个业务工具全部 AVAILABLE，`/api/v1/capabilities` 的 `ml_resources`、`ml_context`、`ml_tools`、`coordination` 全为 true；重复运行应复用原进程，`start-all.ps1 Stop` 沿用原停止流程。启动就绪只能证明当前配置和服务可用，不代替真实 Provider 对话、GPU 推理或 ML 训练验收。

## 证据能说明什么

- Mock LLM/Runtime 验证协议、状态与错误处理；它不证明自然语言理解或材料预测准确性。
- 真实 Provider 验证模型选工具和补参行为；若工具仍是 Mock，不能据此宣称真实 SEM、EBSD 或 ML 计算通过。
- 独立 Runtime 的 GPU 测试验证模型适配和产物合同，不覆盖平台持久化、恢复或界面操作。
- 浏览器验收检查真实点击、键盘、焦点和窄屏布局；jsdom 断言不能替代它。科学准确性还需要独立数据与评估。

测试命名沿用现有约定：Python 为 `test_*.py`，前端为 `*.test.ts`。

## 最小完整容错

`backend/tests/unit/test_agent_reliability.py` 注入临时错误、鉴权/额度错误、Retry-After、预算耗尽、流式半途断连、迟到分片、整段模型超时和退避停止，验证恢复原 Run 后不重复已成功工具。`test_mcp_executor.py` 验证安全只读查询最多三次且仍为一个 Invocation，写操作未知不重新派发，完整回执与身份回执有不同恢复资格。

`backend/tests/api/test_agent_recovery_api.py` 使用真实 PostgreSQL，注入提交前失败和提交回包丢失，检查答案原子性、原操作身份、恢复版本及同进程遗留运行修复；并发回归覆盖扫描期间的新提交与人工恢复，确认正常答案仍能提交。`backend/tests/api/test_ebsd.py` 检查传输结果未知时 Invocation 确实持久化为 `OUTCOME_UNKNOWN`、核查不重新执行，以及存在未决 Managed 记录时拒绝降级数据库。`backend/tests/integration/db/test_migrations.py` 覆盖迁移 head 与空历史的升降级往返。`backend/tests/integration/storage/test_minio_storage.py` 包含真实上传回包丢失后核查同一对象，以及核查不可用时禁止重传/删除。前端 `agent-recovery.test.ts` 验证恢复动作、待核查按钮、Retry-After 倒计时与预算耗尽禁用。

运行这些测试后，再执行完整 Backend 回归和单独的 Mock Runtime 回归；涉及 MCP 时，用独立 ML 环境开启 `ML_INTEGRATION=1` 执行 `test_platform_mcp.py`、`test_mcp.py`。这些故障由替身或受控边界注入，不会关闭共享 PostgreSQL/MinIO 服务；不能据此宣称真实 Provider/GPU 或浏览器断网与进程崩溃场景已通过。

## 研究过程流式展示

相关测试：`backend/tests/unit/test_agent_process.py` 验证分片、脱敏、晚到消息归并与调度交接；`backend/tests/api/test_agent_process_stream.py` 验证 SSE、所有权、历史、显式恢复、回执与删除；`test_agent_sdk_recovery.py` 保留 SDK 崩溃窗口重放验证。

前端 `assistant-markdown.test.ts` 覆盖完成态原文、数学延迟渲染和不安全输入；`process-streams.test.ts` 覆盖重连归并、旧连接隔离与折叠选择；`app-flow.test.ts` 覆盖聊天提交和回答展示。

## 真实链路验收

- 流式界面：使用有延迟分片的模型检查完成前内容可见、过程展开/折叠、Markdown 与公式、向上阅读和窄屏；刷新或断网重连后恢复同一 Run，核对后端工具次数，确认没有重复派发。
- 容错与恢复：在隔离环境注入模型超时、退避等待和提交回包丢失，检查暂停、Retry-After、停止、原 Run 继续与预算累计；后端进程重启后核查原回执，不能仅以页面恢复或健康探针代替执行结果验证。
- 真实模型与 ML：分别使用真实 Provider、Python 3.8 GPU Runtime、独立 ML Service/Worker 环境；ML 训练/预测及真实浏览器入口见 [ML 服务说明](../services/materials_ml/README.md#独立服务真实验收)。合成数据、测试模型与独立 GPU 通过结果不能推导真实 Provider 驱动的完整科研流程或科学准确性。

单次验收在对应 Issue 或交付回复中记录版本、环境、实际执行场景、结果与未覆盖项。历史验收可从 Git 历史追溯，不能据此推导当前部署或当前测试结果；默认不为每次验收新增长期报告文件。
