# Materials Agent — Project Context

本文记录现役架构和维护约束。事实优先级为负责人当前指令、代码/迁移/测试/运行行为、README、本文，
最后才是 Git 历史。README 负责使用入口，AGENTS 负责修改纪律；本文不记录阶段进度或某次测试计数。

## 系统边界

单用户、本地运行的模块化单体。Backend 使用 Python 3.11，真实 ZTA35G Runtime 使用隔离的
Python 3.8 和旧模型依赖。SEM 是只读外部模型包；Backend 不加载权重、不安装旧模型依赖。

默认 Registry 注册材料单位换算 Standard Tool 和 ZTA35G SEM 虚拟实验 Managed Tool。请求宿主
同步推进 AgentRun；AgentRuntime 应用层不依赖 HTTP，ZTA35G 模型 Runtime 通过受控 HTTP 合同接入。
不引入 Worker、队列、Planner、多 Agent、WebSocket、
SSE 或任意 checkpoint 接管。生产 Catalog 拒绝 Side-effect Tool。

```text
用户目标 → AgentRun → DecisionEngine → Structured AgentAction
                          ↑                 ├─ CallTool → Registry/Resolver → Invocation → Executor
                          │                 │                                      ↓
                          └──── Observation ┴─────────────────────────────────可信工具结果
                                            ├─ AskUser → 持久化等待 → 用户输入恢复
                                            └─ Finish → Final Response Policy → FinalAnswer
```

## 领域分层

AgentRun 保存目标、来源消息、版本和 claim、预算快照、累计 Token/活跃时间/执行数、Steps、等待项、
参数草稿、Invocation 关联、Observations、FinalAnswer 和重试来源。Step 是有序决策尝试；失败尝试
也占步骤额度。DecisionEngine 只接受 CallTool、AskUser、Finish 三种动作，未知动作终止运行。

Standard Tool 只创建 Invocation。Managed Tool 保留 Task → InputRevision → ToolRun → ToolResult →
Asset 子任务链路。每个 Managed 调用有自己的来源身份，不能拼接不同 ToolRun 的性能、图像或解释。
Task 的终态以工具结果为准，回答生成失败不回写 Task 或损坏工具产物。

Invocation 的幂等键来自 AgentAction（Step ID），不再以单条消息为边界；同一消息允许多个有序
动作。agent_execution 用 action_id 主键与 invocation_run_id 唯一约束记录对应关系。

旧 Router、消息补参服务、固定 Explanation、旧 Task/Invocation 写 API、Native Tool Calling
独立执行路径已删除。现役 schema 仍保留旧 llm_call、natural_language_explanation 等表及映射；
Agent Loop 的模型计量和最终回答分别写入 agent_model_call、agent_final_answer，不走旧解释生成链。
运行记录通过 agent_runs 路由和前端 api/agent 查询；结果、资产和对话共享类型保留在 api/types。

## 状态与事务

PENDING → RUNNING → WAITING_FOR_USER / WAITING_FOR_CONFIRMATION / SUCCEEDED / TERMINATED。
两个业务等待状态可恢复，其他终态保持只读。FinalAnswer、assistant Message 和 Run 成功状态在
同一短事务提交。HTTP 发送失败不能回滚业务成功；重放返回已保存答案。

Run version + claim 决定唯一推进者；Invocation 另有 version/status/claim CAS。派发 marker 在
外部调用前提交。LLM、Runtime 和 MinIO 调用期间不持有数据库事务或锁。外部结果返回后的领域
提交会复核当前 Run 所有权，过期执行者不能覆盖终态或新状态。lease 到期不允许重新派发。

现有 Managed ResultService 和 Invocation 使用独立短事务，结果与 Observation 存在分段提交。
Runtime 在下一次决策前实施一致性门禁；丢失返回值时，Gateway.repair 仅从已提交的 Invocation /
ToolResult 确定性构造 Observation。Observation ID、唯一约束、Run CAS 使修复幂等。修复不执行
Runtime、不重新上传 MinIO、不调用 LLM；无法修复则终止并保留已有产物。

启动时把旧进程的 PENDING/RUNNING Run 标记为 PROCESS_INTERRUPTED，未完成模型调用按已预留
额度估算消耗，不接管执行。启动时先从已提交结果补全缺失 Observation，再终止旧 Run；不会重新派发。可信结果可以通过受限回答再生成重新组织回答。

## 工具参数与人工参与

INTENT_CLARIFICATION 澄清目标或下一步；TOOL_ARGUMENT_CLARIFICATION 针对已经绑定的工具。
Agent 可以主动询问真实存在且未可靠确定的字段；应用检查工具绑定、Schema 字段、已知值和
Resolver 状态。公共补参问题由实际字段生成，避免模型虚构必填条件、范围或默认值。

独立 ToolArgResolver 负责规范化、合并和校验。初次 CallTool 直接校验候选参数，不额外调用 LLM。
恢复时仅让 tool_arg_resolution 模型从用户新增输入提取一次增量，然后确定性处理。
Missing / Invalid / Conflict / Ambiguous 成为参数事实，Agent 不能自行覆盖；参数解析结果作为
Observation 回流。Managed 使用 Task/InputRevision，Standard 使用 Run 参数草稿。
Resolver 已确定参数状态后，AskUser 可以原样复述无 issue 的规范化已知值，但不会合并回草稿；
改值、为未解决字段补值或询问已可靠确定的字段仍按协议冲突终止。

AskUser 持久化 waiting_version 后结束当前请求。恢复必须携带原 Run ID、waiting_version 和新增
文本，不能悄悄更换工具或重置预算。需要确认时绑定 Invocation、有效参数指纹和确认版本；确认
时重新授权，参数变化、过期或拒绝终止运行。仅 test/dev Fake Tool 验证副作用确认流程。

## 最终回答与重试

Final Response Policy 根据 Finish 的完整答案、可直接展示的 Presenter 结果以及是否需要综合
Observation 决定是否调用 FinalAnswerGenerator。Managed/Standard 类型不决定生成次数。
所有最终回答必须经过 Finish 和 FinalAnswer 持久化。

Tool Retry 仅针对失败且可重试的 Invocation，创建新 AgentRun 和 Invocation 并记录来源；Managed
重试沿用原 Task、有效 Revision，生成新 attempt 和 seed。结果不确定的超时或协议失败不能作为
可安全重试失败。成功结果重算属于新目标，不提供 RECOMPUTE 类型。

Answer Regeneration 只导入可信成功 Observation，设置 tool_execution_disabled，给 DecisionEngine
空工具集合；模型若返回 CallTool 则按协议终止，执行边界再次拒绝派发。仍经过决策、Finish、
Final Response Policy、预算和持久化流程。

## 预算与模型适配

默认预算：12 个动作步骤、4 次工具执行、3600 秒活跃时间、32000 Token；Standard timeout 10 秒，
ZTA35G timeout 1200 秒。创建 Run 时固化配置；等待用户/确认不计活跃时间，恢复不重置预算。
外部调用 timeout 取单次配置与 Run 剩余时间的较小值。超时不宣称远端 GPU 已被取消。

三种模型角色为 agent_decision、tool_arg_resolution、final_answer，受控 DeepSeek/Qwen 目录和
参数能力声明位于 backend/config/llm.toml。LangChain 仅实现模型适配，不能独立决定工具执行。
MockAgentModel 只实现固定离线规则和 JSON 补参增量；自然语言多工具编排由 Provider 决策验证。

统一 Usage 优先 Provider input/output/reasoning/total；reasoning 已包含在 completion/total 中时
不重复累计。缺失 Usage 使用版本化 cl100k-x2-or-utf8-framing-v1 保守估算，标记 estimated；不能
按零继续。使用项目 cl100k 计数两倍和 UTF-8 字节上界中的较小值，离线词表不可用时使用字节上界。估算覆盖实际出站 messages、Action/Tool Schema、封装余量和输出预留。调用前动态约束
实际 max_tokens 与 thinking_budget，剩余额度不足则不请求 Provider。无货币预算。

Conversation Context 来自同一对话的已完成 Run，按完整用户/assistant 对保留和裁剪；不同对话不
共享上下文。Context 不提供自动实验参数继承许可，只有明确引用历史条件时才能使用。工具输出和
用户文本是数据，不能覆盖系统指令。Trace 保存动作、状态、Usage、摘要与来源，不保存完整 Prompt、
Provider 原文、隐藏思维链、图片 bytes、内部路径、权重或 Secret。

重复执行指纹由 Tool ID、版本、schema hash 和规范化有效参数组成；同一 Run 成功执行过相同指纹
则 DUPLICATE_TOOL_CALL 终止。Backend 自动生成的 seed 不进入指纹，不能绕过检测。

## Tool 与 Runtime 合同

Registry 决定版本、Schema hash、执行绑定和授权。ToolDefinition 只承载 JSON 元数据，执行对象由
ToolExecutionBinding 提供。输入必须通过声明的 validator/normalizer，输出通过受控 codec/Presenter。
Standard 和 Managed 使用同一个 Registry、Invocation 与 ExecutorRouter。

ZTA35G 固定 num_samples=1、guide_scale=2.0、timesteps=1000。执行一次实验生成底层 SEM 表征，
按 requested_outputs 返回 sem_image、mechanical_properties；仅请求性能时图像可为 intermediate。
返回值区分 SUCCEEDED、PARTIALLY_SUCCEEDED、FAILED，completed/failed 必须覆盖 requested。
Backend 不自动重试 Runtime execute，真实模型依赖只在隔离 Runtime 环境。

MinIO 上传经过 Asset 生命周期和内容完整性验证；公共图片只通过资产 ID 和受控 content URL 读取。
Conversation 删除先核实归属和活动状态，再原子删除数据库聚合，最后事务外根据持久化 cleanup
记录删除已确认属于本项目 namespace/bucket 的生成对象。禁止清空共享卷或触碰 SEM/权重/凭据。

## API 与前端

POST conversations/{id}/messages 使用 NEW_RUN 或 RESUME_RUN。响应以 agent_run 为顶层业务对象。
GET agent-runs/{id} 查询状态；GET conversations/{id}/agent-runs 使用游标分页；GET agent-runs/{id}/trace
分页读取 Step、Observation、ModelCall。确认/拒绝定位 Run + Invocation，retry 显式选择失败调用或
回答再生成。具体字段以 FastAPI OpenAPI 和契约测试为准。

useAgentRuns 统一提交和恢复，sessionStorage 保留未确认写操作的原幂等键。刷新/轮询只读，网络
不确定时检查原提交，不新建重复目标。UI 沿用聊天输入、结果摘要、AssetGallery 和删除对话框。
切换对话与恢复目标隔离输入草稿，中文输入法 Enter 不误提交；长执行显示已提交状态。

本地栈由 scripts/dev/local-dev.ps1 管理；Mock 与真实模式独立选择。Vite 代理允许 3660 秒请求，
HTTP 宿主断连不等于业务取消。统一本地门禁为 scripts/acceptance/run-agent-acceptance.ps1，依赖
已运行的 PostgreSQL/MinIO；离线仅指不调用真实 Provider/GPU。手动验收场景见 README。
真实 Provider、GPU 和浏览器验收应分别陈述证据，不能由静态配置或单元测试推断运行成功。
