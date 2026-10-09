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
   ├─ Mock Runtime（Python 3.11）或真实 ZTA35G/EBSD/TC4 Runtime（Python 3.8）
   └─ 可选 Materials ML Service / Worker（独立 Python 3.11）
```

Backend 是业务协调者；真实 Runtime 是旧模型和 GPU 依赖的隔离层。ML Service 有独立的存储与 Worker，不负责推进平台的 AgentRun。代码入口分别在 `backend/src/materialsagent/main.py`、`zta35g-runtime/src/` 和 `services/materials_ml/src/`。

日常入口 `scripts/dev/start-all.ps1` 通过同一 `local-dev.ps1 -AllFeatures -Runtime Real -Llm Provider` 托管完整栈，开启 TC4、ML 工具、CSV 资源和可信资源上下文。模型路径从参数、进程环境或根 `.env` 读取；TC4 权重与持久回执目录只传给 Python 3.8 Runtime。ML 配置预检与实际 Backend 使用相同的功能覆盖；启动及重复启动检查全部业务工具为 AVAILABLE，并检查 ML 资源能力。缺少依赖时明确失败，原脚本的按需和 Mock 入口继续保留。

## 一次聊天如何完成

1. 前端从 `ChatComposer` 提交消息和可选附件。`POST /api/v1/conversations/{id}/messages` 先返回接收回执；后端独立调度对应 AgentRun，前端通过 SSE 查看过程，列表查询用于历史和状态补查。上传不确定时使用原幂等键核查，避免重复写入。
2. `AgentRuntime` 从对话、Run 和授权资源构造受控上下文。`ContextFramework` 只投影本次模型需要的事实；模型看到临时资源引用，不直接看到内部存储路径、凭据或完整原始结果。
3. LangChain `create_agent` 使用 Provider 原生工具调用续轮；LangGraph checkpoint 保存 SDK 消息和暂停游标。业务校验、预算、授权和执行权仍由 Backend 的 Agent Runtime 与 Tool Registry 控制。模型可调用 `ask_user` 暂停；用户回复后恢复原 Run。
4. 工具请求经参数、资源身份、权限和状态校验后执行。Standard Tool 记录 Invocation；Managed Tool 还记录 Task、InputRevision、ToolRun、ToolResult 与附件来源。已验证的 Observation 返回模型继续处理。
5. 模型不再请求工具时，其完整助手消息作为最终回答。Backend 将回答、可信来源和 Run 成功状态提交；前端把进度、消息及结果附件呈现在同一对话中。

相关实现主要位于 `backend/src/materialsagent/application/agent_runtime.py`、`sdk_agent_loop.py`、`context_framework.py`、`tools.py` 和 `frontend/src/composables/`。

## 研究过程与流式协议

`AgentRuntime` 托管执行与正式回答提交，`SdkAgentLoop` 消费 SDK 的流式消息；`AgentProcess` 负责公开过程段、内容快照和订阅通知。`useProcessStreams.ts` 管理前端 EventSource，`ResearchProcess.vue` 与 `AssistantMarkdown.vue` 统一展示实时和历史内容。术语见 [词汇表](../GLOSSARY.md)；保存内容与执行生命周期的取舍见 [ADR-0001](adr/0001-retain-research-process-content.md) 和 [ADR-0002](adr/0002-run-independent-of-browser-connection.md)。

公开过程保存在独立的 `agent_process` 表中，随所属 Run 级联删除，与 SDK checkpoint 分别维护。过程段有稳定身份和 revision，区分模型思考、行动说明、工具事实与未完成内容；工具事实只从实际业务执行记录投影。文本和思考经跨分片尾部缓冲及已知私有值脱敏后才进入 SSE 和存储，已收口的段不能被迟到分片重新打开。保存累计内容，不保存逐 token 播放轨迹；更新时约每秒保存一次，收口时强制保存，突然断电仍可能丢失自上次保存以来的内容。

`GET /api/v1/agent-runs/{id}/process` 读取公开快照，`/events` 通过 SSE 观察原 Run，沿用原访问权限校验。服务端先注册订阅再读取快照；每次连接（包括携带旧 Last-Event-ID 的重连）先发送完整 `snapshot`，随后 `process.updated` 发送变更段的累计完整内容，本轮执行收尾时发送 `settled`。客户端按 epoch 和段 revision 归并，忽略旧连接和旧修订；事件 id 不用于逐 token 重放。慢客户端读取最新累计状态，订阅断开不取消执行，重连不创建或重新派发任务。

Markdown 段接收结束、模型调用结束和任务完成是不同阶段；正式回答与 Run 成功状态一起提交后，保存正文替换实时预览。历史与复制使用公开原始 Markdown，显示修补不写回记录或模型上下文。模型思考和行动说明不作为科研证据。

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

默认 Registry 注册单位换算、ZTA35G SEM 虚拟实验和 EBSD 屈服强度预测；可选 TC4 与 ML 工具由配置启用。真实 SEM、EBSD 与 TC4 共用 Python 3.8 Runtime GPU 执行锁，Backend 不加载权重，也不自动重试模型执行。EBSD 输入图片是来源，不是生成结果；RGB、正方形等专属要求在 EBSD 派发前校验。

TC4 注册为 `tc4_primary_alpha_segmentation` Managed Tool。模型提交临时图片引用数组，Backend 的统一资源解析器将其绑定成 `ResourceCollectionBinding`，冻结有序的资源身份及内容摘要，单资源合同保持有效。新上传使用通用 `image` 类型，兼容历史 `ebsd_image` 与单附件消息；CSV 单独提交。图片内容与本机路径、权重路径和存储凭据不进入模型上下文。

上传与 TC4 Runtime 同时兼容完全不透明的 8 位 RGBA PNG：检查 PNG 原始编码位深，并逐像素确认 alpha 为 255（包括 RGB/灰度 PNG 的 `tRNS` 透明色），保存原始文件与摘要，推理时沿用 RGB 转换。实际透明或半透明像素拒绝处理，避免未定义背景影响面积统计；不对原文件裁剪、增强或重新编码。EBSD 专属 RGB 与正方形校验保持在其派发前执行。

一次批量只有一个 Invocation、Task、InputRevision、ToolRun；`tool_run_item` 保存每项输入、序号、稳定 Runtime 请求身份、版本、状态、原回执和成果关联。`local_tc4` 在每次派发前检查执行权、持久停止状态和原 Run 时间预算；提交 DISPATCHED 后才发送请求。停止后只收已派发回执，不启动下一张。单张输入失败可继续；Runtime/GPU 故障或未知结果暂停。恢复沿用原父记录，先查询已派发项的原回执或补存已收到的成果，再处理未派发项，不增加 Agent 工具执行次数。

Python 3.8 Runtime 自有 ResNet50 U-Net 推理模块保持原 RGB/512 letterbox/归一化/概率图恢复/argmax 顺序，模型每进程加载一次，FP32/eval/no_grad。同步单图执行在推理前落盘未知回执，终态回执与两个 PNG 原子保存到配置的数据目录。重复身份只读取原操作；重启遗留未知计算也不重跑。Backend 校验回执身份、模型版本、PNG 摘要/模式/尺寸及掩膜前景像素数，再逐张保存统一 Asset。成果关联输入、ToolRunItem、模型与预处理版本。

整批结束才提交专属 ToolResult，成功/部分成功/失败按图片项计算；SEM/EBSD 原有按输出种类统计保持不变。已有图片成果可在父结果形成前授权查看。`0027_tc4_batch_segmentation` 新增批量项、图片来源字段和 Runtime 回执清理队列；对话删除事务先将可证明归属的终态请求写入独立 outbox，再删除业务记录。Runtime 清理核对原身份、拒绝运行中或未知操作，保留删除标记防止复用身份重跑。失败的清理留待后续启动/删除 drain。

沿用 LangChain 的通用工具循环，确定性的逐图顺序、停止与原回执恢复由业务适配层实现，不新增通用调度框架。工具定义/执行解耦参考 [deepseek-harness 工具规范](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/tools.md)和 [LangChain 工具文档](https://docs.langchain.com/oss/python/langchain/tools)；整图范围见 [ADR-0003](adr/0003-tc4-whole-image-batch-segmentation.md)。

ML Engine 只做数值表格回归，目前支持 LR/RF。Service 管理数据、训练和预测，Worker 通过 Service API 交回结果。平台只持有受控资源引用；模型对资源名称和单位的推断须由 Backend 对最终绑定资源再次校验，未知单位在数值换算前需要用户明确确认。

## 界面边界

前端以聊天为唯一工作入口：问题在原输入框补充，运行状态和结果跟随消息展示，附件详情通过只读 Viewer 查看。查看或下载不改变工具输入、草稿或 Agent 状态。界面文案使用用户能理解的进度和错误，不暴露内部 ID、原始 JSON 或调试异常。样式与响应式规则以 `frontend/src/styles.css` 和组件测试为准。

历史消息分页独立于最近 Run 列表；加载到较早回答时，前端补查该消息页中尚未加载的关联 Run，恢复逐图状态、统计和分组图片，不重新派发工具。

正文使用 Markdown；模型思考、行动说明和工具事实分开标注，完成后默认折叠，并保留用户主动展开的选择。`AssistantMarkdown` 使用 remend 修补流中显示副本，完成后由 markdown-it 直接解析原文，KaTeX 在所属段接收完成后排版公式。渲染禁用原始 HTML，限制链接协议并隔离外链；Markdown 图片只显示替代文字，结果图片通过原受控附件展示。用户向上阅读时不强制滚回底部。该实现依赖单 Backend 进程的运行托管与广播，不能直接以多个独立 Uvicorn worker 部署。
