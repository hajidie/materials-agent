# 架构与数据流

本文解释现有代码怎样完成一次聊天任务，以及修改时需要守住的边界。具体字段和接口以当前路由、Pydantic schema、迁移及合同测试为准；本文不复制完整 API 清单。

## 系统组成

```text
Vue 聊天界面（frontend/）
        │ HTTP / SSE /api/v1
        ▼
FastAPI Backend（backend/，Python 3.11）
   ├─ PostgreSQL：对话、消息、AgentRun、工具来源、公开过程快照与 SDK checkpoint
   ├─ MinIO：上传图片和生成附件
   ├─ Mock Runtime（Python 3.11）或真实 ZTA35G/EBSD Runtime（Python 3.8）
   └─ 可选 Materials ML Service / Worker（独立 Python 3.11）
```

Backend 是业务协调者；真实 Runtime 是旧模型和 GPU 依赖的隔离层。ML Service 有独立的存储与 Worker，不负责推进平台的 AgentRun。代码入口分别在 `backend/src/materialsagent/main.py`、`zta35g-runtime/src/` 和 `services/materials_ml/src/`。

## 一次聊天如何完成

1. 前端从 `ChatComposer` 提交消息和可选附件。`POST /api/v1/conversations/{id}/messages` 先返回接收回执；后端独立调度对应 AgentRun，前端通过 SSE 查看过程，列表查询用于历史和状态补查。上传不确定时使用原幂等键核查，避免重复写入。
2. `AgentRuntime` 从对话、Run 和授权资源构造受控上下文。`ContextFramework` 只投影本次模型需要的事实；模型看到临时资源引用，不直接看到内部存储路径、凭据或完整原始结果。
3. LangChain `create_agent` 使用 Provider 原生工具调用续轮；LangGraph checkpoint 保存 SDK 消息和暂停游标。业务校验、预算、授权和执行权仍由 Backend 的 Agent Runtime 与 Tool Registry 控制。模型可调用 `ask_user` 暂停；用户回复后恢复原 Run。
4. 工具请求经参数、资源身份、权限和状态校验后执行。Standard Tool 记录 Invocation；Managed Tool 还记录 Task、InputRevision、ToolRun、ToolResult 与附件来源。已验证的 Observation 返回模型继续处理。
5. 模型不再请求工具时，其完整助手消息作为最终回答。Backend 将回答、可信来源和 Run 成功状态提交；前端把进度、消息及结果附件呈现在同一对话中。

相关实现主要位于 `backend/src/materialsagent/application/agent_runtime.py`、`sdk_agent_loop.py`、`context_framework.py`、`tools.py` 和 `frontend/src/composables/`。

## 状态、失败与重试

`AgentRun` 是业务状态的权威记录，checkpoint 只负责模型续轮。Run 可等待用户补充或确认；短事务、版本和 claim 控制唯一推进者。外部 LLM、Runtime、MinIO 调用不应跨越数据库长事务。已派发操作发生断连或超时时，只核查原回执，不因为请求失败而重新派发。

浏览器断线或关闭不取消后台任务；重连从累计内容快照恢复查看。后端重启把旧活动 Run 标为 `INTERRUPTED`，用户点击继续后先核查原回执；等待补充/确认的 Run 保留等待状态。公开过程独立于 SDK checkpoint，终结清理 checkpoint 不会删除历史过程。

Stop 取消后续模型决策；已派发的 GPU 或 ML 工作可能继续，迟到结果仍保存。失败工具的显式重试创建新的执行尝试；结果不确定时不能假设可安全重试。回答重新生成使用原任务和已验证结果，禁止调用工具，并保留旧回答版本。删除对话先检查运行和资源状态，再通过持久化清理记录处理本项目对象。

### 失败与恢复边界

`SdkAgentLoop` 使用 LangChain `ModelRetryMiddleware`，内层负责每次请求的持久化和流关闭，外层管理整次逻辑调用的时间窗口与取消。Provider SDK 重试为 0；默认首次请求加两次重试，指数退避 1、2 秒，上限 4 秒并带抖动。仅连接、超时、临时限流和可恢复服务端故障自动重试。鉴权、额度、配置错误暂停；协议、权限、来源不一致和未知内部异常停止。有效 `Retry-After` 保存最早恢复时间并暂停。

`ModelCall` JSON 记录逻辑调用标识、尝试序号、失败分类和用量。每次实际请求前提交预算预留；失败消耗未知时按输入估计与输出预留保守计费。失败流立即转为未完成的过程记录，迟到分片不能重新打开它，也不进入 SDK 消息历史或最终答案。停止覆盖请求和退避等待；`ask_user` 仍走 LangGraph 中断。

Registry 的内部 `auto_retry_safe` 默认关闭，冻结到 Invocation 策略快照。首版仅 ML 数据概况和训练状态查询开启；LangChain Core 非流式 Runnable 在远程读取层最多请求三次，共用 Invocation、参数绑定和总截止时间。训练提交、持久化预测、SEM 和 EBSD 不自动重新执行。已派发而响应未知的操作保留 `pending_execution` 与 `Invocation.OUTCOME_UNKNOWN`，不生成确定失败的 Observation，不覆盖 Task/ToolRun 的最后可证实事实。

临时模型故障耗尽、配置待修正、依赖故障和结果未知保留 checkpoint，以 `INTERRUPTED` 暂停。`/resume` 使用原 Run、submission、版本与 claim；所有尝试累计消耗原预算，每次人工继续开启新的有限窗口。成功工具只重放已验证回执。核查入口也支持待执行记录；完整结果与冻结请求一致后才能幂等写入，单独确认资源身份不会解锁后续步骤。没有远端查询能力时保持待核查。

纯持久化按原操作身份、版本、对象键和幂等键核查；确认已提交就复用，确认未提交才最多重试两次（等待 0.5、1 秒）。外部计算不在这个重试范围内。答案与成功状态保持同事务；预留或派发记录保存失败时不发出新的外部请求。每 5 秒的恢复扫描排除仍有推进者或回执任务的 Run，只修复遗留运行状态。提交、回复、确认、重试、重新生成与人工恢复通过 Runtime 在同一进程锁内完成待运行状态写入和推进者登记，扫描使用相同锁；模型和工具执行不持有该锁。重启和扫描都不自动继续模型或工具。对象上传核查原对象内容与元数据，不创建新身份或删除无法确认归属的资源。

公开 Run 返回 `can_resume`、`recovery_action` 和可选 `resume_after`，前端据此显示继续、核查、修正配置后继续或结束；服务端继续执行预算、时间、所有权、版本和幂等校验。元数据扩展现有 JSON，旧记录使用默认值，没有新增表或状态枚举；历史 `TERMINATED` 不追溯恢复。

`0026_managed_outcome_unknown` 同步数据库约束，允许 Managed Runtime 的未知结果保存为 `Invocation.OUTCOME_UNKNOWN`；已有此类未决记录时拒绝降级，避免丢失执行证据。部署这版代码前必须升级 Backend 数据库。

框架能力参考 [LangChain 中间件文档](https://docs.langchain.com/oss/python/langchain/middleware/built-in)；重试前落盘的边界参考 [deepseek-harness llm-retry](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-retry/README.md)。本项目的预算、执行身份、成果提交和恢复资格由 Backend 管理。

## 工具与资源边界

默认 Registry 注册单位换算、ZTA35G SEM 虚拟实验和 EBSD 屈服强度预测；可选 ML 工具由配置启用。真实 SEM 与 EBSD 共用 Runtime 执行资源，Backend 不加载权重，也不自动重试模型执行。EBSD 输入图片是来源，不是生成结果；结果必须保留对应 ToolRun、单位和适用限制。

ML Engine 只做数值表格回归，目前支持 LR/RF。Service 管理数据、训练和预测，Worker 通过 Service API 交回结果。平台只持有受控资源引用；模型对资源名称和单位的推断须由 Backend 对最终绑定资源再次校验，未知单位在数值换算前需要用户明确确认。

## 界面边界

前端以聊天为唯一工作入口：问题在原输入框补充，运行状态和结果跟随消息展示，附件详情通过只读 Viewer 查看。查看或下载不改变工具输入、草稿或 Agent 状态。界面文案使用用户能理解的进度和错误，不暴露内部 ID、原始 JSON 或调试异常。样式与响应式规则以 `frontend/src/styles.css` 和组件测试为准。

研究过程与流式协议详见 [流式展示设计](design/streaming-research-process.md)。正文使用 Markdown；模型思考、行动说明和工具事实分开标注，完成后默认折叠，公式在所属消息接收完成后渲染。该实现依赖单 Backend 进程的运行托管与广播，不能直接以多个独立 Uvicorn worker 部署。
