# Materials Agent — Project Context

本文记录现役架构和维护约束。事实优先级为负责人当前指令、代码/迁移/测试/运行行为、README、本文，
最后才是 Git 历史。README 负责使用入口，AGENTS 负责修改纪律；本文不记录阶段进度或某次测试计数。

## 系统边界

单用户、本地运行的模块化单体。Backend 使用 Python 3.11，真实 ZTA35G Runtime 使用隔离的
Python 3.8 和旧模型依赖。SEM 与 EBSD 研究目录是只读外部模型包；Backend 不加载权重、不安装旧模型依赖。

默认 Registry 注册材料单位换算 Standard Tool、ZTA35G SEM 虚拟实验与 EBSD 屈服强度预测 Managed Tool。请求宿主
同步推进 AgentRun；AgentRuntime 应用层不依赖 HTTP，ZTA35G 模型 Runtime 通过受控 HTTP 合同接入。
Agent 平台本身没有后台 Worker、队列、Planner、多 Agent、WebSocket、SSE 或任意 checkpoint 接管；
可选 Materials ML 训练 Worker 是独立领域进程，不推进或接管 AgentRun。生产 Catalog 拒绝 Side-effect Tool。

```text
用户目标/附件 → ContextFramework → DecisionEngine → Semantic AgentAction Proposal
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
训练 Proposal 不开放 `units`：该远端可选字段是已登记单位的相等断言，不是单位声明入口。
ML Service 从数据集不可变元数据继承单位；null 表示未登记。Agent 可从字段名或用户原文推断单位，
通过 Proposal/Tool Argument 的独立 `semantic_annotations` 表达，来源只能为 `model_inference`。
Backend Unit Resolution Policy 核验最终绑定资源的列和登记信息，将注解与执行参数分离保存到
ArgumentDraft、ExecutionRecord 和 Observation；不传入 ML `units`，不写回 Dataset。
确定性有效性只基于已绑定资源、真实数值字段、受支持的单位系统以及 declared/confirmed metadata。
所有仅由模型推断且未声明/确认的单位都进入通用补参流程；evidence 仅是 provenance 与结果说明，缺失、自由措辞或不包含字段名都不单独决定有效性。
已登记/可信确认事实优先，冲突保留说明；执行确认不等于单位确认。
换算的单位澄清回复由 Tool Argument 提取明确的 from_unit/to_unit 增量，Backend 仅在当前单位问题
中建立临时 confirmed 事实；未明确单位的回复不解除限制，改变数值单位参数会使该确认失效。
Result Projection 保留 provenance 和原文依据，最终消息与只读 Viewer 从对应执行事实读取注解；
原始指标与 canonical 单位不变，推断不能冒充已登记事实或跨资源复用。预检领域拒绝按白名单
投影具体原因，不能误报为连接失败；工具失败只展示一次结果说明，不叠加通用 Run 错误。
决策上下文上限为 50000，补参与最终解释为 8192；均扣除 1024 安全余量并预留输出。
完整工具目录与当前附件的首轮请求覆盖 UTF-8 保守估算回归；超限仍裁剪历史与候选，不能绕过 Run 总预算。
MockAgentModel 只实现固定离线规则和 JSON 补参增量；自然语言多工具编排由 Provider 决策验证。

统一 Usage 优先 Provider input/output/reasoning/total；reasoning 已包含在 completion/total 中时
不重复累计。缺失 Usage 使用版本化 cl100k-x2-or-utf8-framing-v1 保守估算，标记 estimated；不能
按零继续。使用项目 cl100k 计数两倍和 UTF-8 字节上界中的较小值，离线词表不可用时使用字节上界。估算覆盖实际出站 messages、Action/Tool Schema、封装余量和输出预留。调用前动态约束
实际 max_tokens 与 thinking_budget，剩余额度不足则不请求 Provider。无货币预算。

Conversation Context 来自同一对话的最近 20 条消息，包括追加的任务终态结果；统一按预算从最旧消息裁剪，不同对话不
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

SEM 与 EBSD 的参数、性能字段集合、固定单位和来源校验位于工具合同；Managed 的执行、事务、
幂等、重试、状态与结果一致性门禁保持共用。性能数值使用 data 中的 `{value, unit}`：SEM 成功性能
输出必须同时有 yield_strength（MPa）和 elongation（%）；EBSD 只能有 yield_strength（MPa）。
SEM 规范化输入现以闭合 JSON Schema 声明原有单位、范围与字段；严格化声明会改变 schema_hash，
历史结果仍可读取，旧绑定恢复/重试仍受原有 schema hash 门禁约束，不回写历史快照。
未完成性能输出时 data 为空，不填入虚假数值。EBSD Presenter 通过原有绑定提供确定性摘要。

EBSD 使用现有 Runtime 的 POST /internal/v1/ebsd/execute 和 GET /internal/v1/ebsd/health/ready，
沿用 X-ZTA35G-Runtime-Token 鉴权。execute 的 Content-Type 是 application/octet-stream，body 为
受限图片二进制；X-EBSD-Request 是闭合 JSON：request_id、task_id、tool_run_id、asset_id、sha256、
seed、tool_id、schema_version。Runtime 独立复核摘要、解码、尺寸与格式，不接受路径或远程 URL。
图片只在 Backend 存储适配器与 Runtime 的受控调用中传输，不写日志或 Provider 请求。

EBSD_MODEL_ROOT 指向外部 CNN_1.pt 所在研究目录；适配层固定摘要和兼容 CNN，预处理为
ToTensor → Resize(128)，FP32/eval/no_grad，无 autocast。不设置 TF32 或其他全局 CUDA 精度选项，
结果只记录现有配置。EBSD 与 SEM 共用执行锁，BUSY 需要显式重试，异常释放锁；EBSD 加载失败不
阻断 SEM。EBSD 来源保存输入资产 ID/摘要、工具版本、模型版本、预处理版本和输入 Revision；
图片是输入来源，不是 ResultAssetLink 生成产物。独立接口测试与原 CNN 对照使用同一环境和精度策略。

MinIO 上传经过 Asset 生命周期和内容完整性验证；公共图片只通过资产 ID 和受控 content URL 读取。
Conversation 删除先核实归属和活动状态，再原子删除数据库聚合，最后事务外根据持久化 cleanup
记录删除已确认属于本项目 namespace/bucket 的生成对象和上传 EBSD 图片。禁止清空共享卷或触碰 SEM/权重/凭据。

EBSD 上传使用专用 Asset 分支：UPLOADED/ebsd_image、actor_id 与 conversation_id 必填，Task 和
producer ToolRun 为空；生成 SEM 分支继续要求 Task/producer 且不允许直接 conversation_id。
迁移 0016 固化两种互斥约束，已有 SEM 数据保持兼容；有 EBSD 资产或清理记录时拒绝降级迁移。
上传键绑定 actor、conversation、内容摘要，PENDING 写入短事务后才调用 MinIO，完成后 CAS 为 AVAILABLE。
同键不同内容拒绝；失败可重放原上传。当前单进程 Backend 的上传活动门禁与 Conversation 行锁配合，
上传进行中删除返回 BUSY，防止删除清理后出现迟到写入；异常释放门禁，未完成资产也由现有删除清理覆盖。

## ContextFramework 与模型边界

只有一个 ContextFramework，应用于 Agent Runtime 业务模型调用；通用模型工厂不依赖此框架。
Profile 声明输入字段白名单、输出 JSON Schema 与安全约束；共享投影、来源映射、隐私过滤和输出校验。
资源读取/权限复核复用 ResourceContextResolver 与已有 Repository/Service，预算与裁剪复用 AgentModelAdapter，
不建立多套 Builder、资源目录或预算机制。

| Profile | 模型输入 | 输出与约束 |
|---|---|---|
| Decision | 用户需求、语义历史、授权工具、资源摘要、结果投影 | CallTool/AskUser/Finish；禁止内部对象参数 |
| Tool Argument | 固定工具 Schema、草稿、受控资源上下文、新增输入 | 仅允许当前工具的参数增量或临时 resource_ref；领域 normalizer 校验值 |
| Result Explanation | 用户问题、投影事实与临时结果标签 | text/sources；无工具执行权限，不接受原始结果 |
| Recovery | 已核验说明、事实、未知项、允许建议 | summary/guidance；仅解释，不能授权重派发 |

当前有前三类模型调用；Recovery 合同已定义，既有确定性回执核查不因此增加模型请求。
Profile 输出校验后才转换为 Runtime 内部动作。临时“结果 N”标签在本次调用内映射 Observation，
模型不会看到真实来源 ID。重试、补参和回答再生成经过同一边界；执行重试只复核原冻结身份。

Result Presenter/Projection 是确定性白名单函数：ToolResult → 用户事实/指标/单位/限制/附件描述 → Profile → 回答。
原始结果只用于执行审计与一致性恢复，不允许普通回答分支读取；投影失败只返回安全说明。
未知警告保留限制提示，已知警告译为自然语言。框架不成为插件系统或通用 UI 框架。

## API 与前端

`POST conversations/{id}/attachments` 接收 multipart 单文件与 Idempotency-Key，复用 CSV 和 EBSD 上传服务。
CSV 沿用 20 MiB 限制；PNG/JPEG 仍要求单帧 RGB、正方形 128–4096 像素、最大 10 MiB，不裁剪重编码。
`POST conversations/{id}/messages` 使用 NEW_RUN/RESUME_RUN 与 `attachments`（最多一个）。
附件包含受控传输引用、类型与名称，计入幂等摘要，保存到来源消息并校验同 actor/Conversation、类型与状态。
EBSD 内部执行仍冻结 Asset 身份，但模型只看到 image_reference；旧 ebsd_asset_id 消息字段不再接受。
上传结果不确定时显式 POST attachments/reconcile 核查原键，不自动重复上传；解除草稿关联不删除文件。

GET AgentRun 返回普通展示 DTO：消息、语义结果、确认摘要与必要传输句柄；不返回原始结果、草稿、调用记录或资源快照。
`GET agent-runs/{id}/trace` 仅在显式本地开发诊断开关下提供。确认/拒绝/重试沿用现有版本与权限边界。
内部 ID 可用于受控 API 寻址，不出现在可见文案或模型上下文。前端仅保留对话列表与聊天工作区。

`ArtifactViewer` 从消息附件/结果卡片打开，提供图片、数据概况、指标、预测预览和受控下载。
查看 GET、预览、关闭与下载不调用模型、不登记资源/血缘、不发布消息、不刷新用户选择或工具绑定。
详情与消息必须同归属；Dataset 下载保留原 CSV，Prediction 下载转换为只含行、数值、单位的 CSV。
没有资源中心、分类管理侧栏、训练配置页面。sessionStorage 的 v2 草稿/待提交格式隔离对话及补参目标，
移除失效 v1 格式；不保存文件内容。中文 IME、原键恢复、焦点和删除 fence 继续由聊天组件统一处理。

本地栈由 scripts/dev/local-dev.ps1 管理；Mock 与真实模式独立选择。任一 ML 功能开关启用时，
同一入口在独立 ML Python 3.11 环境执行迁移并托管 ML Service/Worker，不把其依赖或凭据并入 Backend。
Vite 代理允许 3660 秒请求，
HTTP 宿主断连不等于业务取消。统一本地门禁为 scripts/acceptance/run-agent-acceptance.ps1，依赖
已运行的 PostgreSQL/MinIO；离线仅指不调用真实 Provider/GPU。手动验收场景见 README。
真实 Provider、GPU 和浏览器验收应分别陈述证据，不能由静态配置或单元测试推断运行成功。

## Materials ML 领域边界

### 当前已实现：独立 Engine、ML Service、Windows Worker 与 MCP

`services/materials_ml/` 独立安装在 Python 3.11 环境，不进入 Backend 依赖；本地一键入口只负责
在既有独立配置和资源上执行迁移、启动、健康检查与停止 Service/Worker，不执行 provision。
`materials_ml` 保持纯 Engine；`materials_ml_service` 包含 Resource API、Application、Domain、
PostgreSQL/MinIO Adapter、独立迁移与 HTTP Worker。当前闭环为上传 Dataset → 提交 TrainingRun →
Worker 训练 → 发布 ModelAsset → 同 scope Dataset 输入 → 请求监管 Prediction → 查询/下载结果。
独立 MCP 入口提供四个 Tool；平台已有默认关闭的 MCP Executor 与显式合同注册，
平台另有受控 ResourceRef/上传代理与 Conversation 删除协调；可信资源上下文另有默认关闭的开关，聊天页通过统一附件与消息结果卡片访问资源。

所有 ML 资源使用不透明 `scope_id`，不导入、查询或创建 Conversation。同 scope 的复合外键约束血缘；
scope 不用于文件系统路径。Backend 从已校验所有权的 Conversation 取得 ID 并作为 scope 传入，
客户端和模型不能覆盖。当前 Resource token 授权本地资源域，不是多用户登录或租户身份系统。

```text
Resource API / MCP → ML Application Layer → Repository / Storage Adapter → ML PostgreSQL / MinIO
Worker → internal API ↗
       → Windows Job Object → gated child → pure ML Engine → local package
```

Application Layer 是唯一权威生命周期写入入口；Repository、Storage Adapter 只执行它的命令。
Worker 仅有 Service URL 和 Worker token，通过内部 API 领取、心跳、读取输入和上传产物；
子进程环境白名单不含 Service/Worker 凭据。Backend 及现有 Managed Task/InputRevision/ToolRun/
ToolResult/图片 Asset 链未修改。当前单用户本地负载使用数据库短事务 advisory lock、version CAS、
唯一约束共同串行化推进；任何 MinIO 调用、模型验证和训练均在事务外。

### Engine 与冻结的训练输入

Engine 普通 Python 接口支持数值表格诊断、LR/RF 单目标监督回归、本地模型包与预测；
`validate_training_input()` 与训练共用同一纯预检，提交入口不执行 fit。
外部 Train/Test Split 之后，只在外部 Train 内执行固定打乱的 5-fold CV，每折独立拟合完整 Pipeline。
最终模型仅拟合整个外部 Train；Test 不参与预处理、CV 或评估后全量重训。
LR 使用中位数填补、StandardScaler、带截距线性回归；RF 使用中位数填补、100 棵树、固定种子和单线程。
默认 test_size=0.2、random_state=42；Test 和每个 CV validation 至少 2 行，所有计算 R² 的分区
目标须非常量，不降折、不改比例、不伪造分数。报告 R²/MAE/MSE/RMSE，CV 标准差仅表示评分波动。

显式选择数值特征/目标；字符串和类别列有诊断，选入训练则拒绝。目标缺失、无穷值、重复列名、
任一拟合分区内全空特征拒绝。IID 是未经验证的假设，重复行不自动去重。
Dataset 单位快照权威，训练继承；重复声明必须完全一致。未知单位为 null 并给出 UNIT_UNKNOWN；
不从列名推断、不换算、不变换 y。P1 的独立预测仍不实施单位匹配。

输入限 UTF-8 CSV、20 MiB、100,000 行、256 列、2,000,000 单元格，超限拒绝。
`table-json-v1` 摘要覆盖完整表的列顺序、类型、行顺序和内容，包括未选列；不包含 DataFrame 标签。
所有 split/CV 索引均为原表零基位置，绑定摘要版本、SHA-256、行数；加载会重建划分并核对。
新数据预测按保存的特征名对齐，可重排列。完整 Python 与格式合同见 `services/materials_ml/README.md`。

### 持久化对象、状态与存储边界

独立数据库/角色默认为 `materials_ml`，bucket 为 `materials-ml`，对象前缀 `ml/v1/`。
初始化只创建全新且明确命名的 ML 资源，不覆盖既有数据库、角色、bucket 或 IAM 身份。
迁移入口属于 ML 包，版本表为 `ml_alembic_version`，不使用 Backend Alembic。
服务运行不持有管理员凭据，Worker 不持有数据库/MinIO 凭据。

| 对象/记录 | 主要内容与不变量 |
|---|---|
| DatasetAsset | 原文件 Artifact、原 bytes SHA-256、表内容身份/行数、解析/schema 版本、列/单位/诊断；AVAILABLE 后不可变 |
| TrainingRun | 同 scope dataset_id、冻结 spec/单位/依赖/摘要、claim/session、heartbeat、attempt、cancel_requested、recovery_required、受控失败码及 model_id |
| ModelAsset | 唯一 training_run_id、manifest、单位/警告、Test 指标、四个 Artifact 引用；仅在验证后 AVAILABLE |
| Prediction | 同 scope model_id/input_dataset_id、冻结输入/模型身份、单位核验、取消意图、受监管执行身份/发布阶段、结果引用；无队列或 Worker claim |
| MLArtifact | 同 scope Dataset、Run 或 Prediction 三者恰一所有者、固定成员名、ObjectStorageRef、状态、版本、checked_at/维护失败码；自身持久化上传/发布意图和清理墓碑 |
| ml_operation | 业务幂等域 + scope + operation + key 唯一（历史列名 auth_domain）；保存请求摘要与原资源身份，终态/删除后仍保留 |

所有业务对象含 id、scope_id、version 和 UTC 创建/更新时间。PostgreSQL 保存索引、状态、冻结合同、
完整性和持久化操作记录；MinIO 保存原始 CSV、Pipeline、完整评估和索引，不在平台表中保存二进制。
`packages/materials_storage/` 的 stdlib-only `ObjectStorageRef v1` 记录 object_id、store_id、
bucket/key/version、SHA-256、size_bytes、media_type 并验证完整性。Adapter 另检查命名空间和对象归属；
它不是授权或公开下载 URL，不迁移图片 Asset，未来 RAG Document Artifact 可复用。

DatasetAsset 与 MLArtifact 均为：
`PENDING → AVAILABLE/FAILED`；`PENDING/AVAILABLE/FAILED → DELETING → DELETED`。
PENDING 不能用于训练或公开下载；意图提交前不写对象。上传结果未知保持 PENDING；
对象写入成功但数据库发布失败时，用原身份核对对象并恢复发布。确定性失败保留受控失败码并安排清理，
不让同一资源重新上传。AVAILABLE、FAILED、删除墓碑的幂等重放均不重建资源。
DELETING 立即拒绝使用，外部删除失败则继续重试；归属不一致拒绝删除，不触碰未知对象。

Service 启动及每 5 秒执行有界维护，每次最多 50 个最久未检查的记录；
数据库不在外部 I/O 期间保持事务。DELETED 墓碑继续检查迟到对象，不因一次不存在而遗忘。
FAILED Artifact 自动进入清理；Dataset 失败身份保留，显式删除可将其收敛为 DELETED。
Dataset 已有任何 TrainingRun 或 Prediction 血缘时删除返回冲突，无自动级联。没有单独 Model/Run 删除；整体生命周期通过中性 scope close 协调，关闭后清理对象并保留必要血缘/幂等墓碑。

### API 与规范化幂等

默认仅监听 `127.0.0.1:8200`。Resource 路径 `/api/v1/scopes/{scope_id}/...` 与
Worker 路径 `/internal/v1/...`、MCP 路径 `/mcp` 各有独立角色/audience 与 token；
启用 MCP 必须配置第三个不同凭据，每次请求认证，session ID 不授权，凭据不能互换。
所有 Worker 任务操作校验任务与 claim，通常还绑定 session/scope；恢复绑定原 claim、scope 和新 session，
只能由已持有本地互斥锁且确认旧进程树停止的 Worker 调用。公开响应不含 claim、对象 key、内部路径或异常详情。

规范化 JSON 使用 UTF-8、键排序、紧凑分隔符、禁 NaN/Infinity，列表保留业务顺序，默认值先展开。
Dataset digest 包含规则版本/operation/scope、原文件 SHA-256/size/确认媒体类型、CSV 解析合同、
去首尾空白且空值归 null 的显示名，以及按实际列补全的单位映射（未知=null）。
multipart 文件名、boundary、凭据、请求时间、HTTP request ID 和幂等键不进入摘要；
相同表内容但不同文件 bytes 仍构成不同上传请求。

Training digest 包含规则版本/operation/scope、dataset_id 与原文件/表内容身份/行数/解析/schema 版本、
特征顺序/目标/算法、展开后的 seed/test_size/固定 5-fold、完整 estimator/预处理参数及合同版本、
单位快照和环境版本。省略默认值与显式相同默认值等价；同键不同摘要返回 409。
同键同摘要返回原资源当前状态；FAILED、CANCELLED、DELETED 不重新执行，新训练须使用新键。
提交只做确定性预检并持久化 PENDING，立即返回 202。

### Worker、发布、取消与恢复

TrainingRun：`PENDING → RUNNING → SUCCEEDED/FAILED/CANCELLED`，PENDING 也可直接 FAILED/CANCELLED。
领取只为 PENDING 分配 claim，不进入 RUNNING。本地命名互斥锁及数据库单活动 claim 约束保证单 Worker。
子进程先等待 stdin 启动握手；Windows Job Object 建立、关联并核实后才发送 start，
Service CAS 确认 RUNNING 后释放门禁。回包丢失查询原状态；Job 建立/关联失败必须先停止子进程再报告 FAILED。
门禁 EOF 退出、KILL_ON_JOB_CLOSE 和显式终止共同管理本次进程树，不降级为无监管训练。

默认轮询 1 秒、心跳 5 秒、lease 30 秒、连续 20 秒无确认停止计算、训练超时 30 分钟。
RUNNING 取消先记录 cancel_requested，进程树停止确认后才 CANCELLED；
PENDING 可立即 CANCELLED，但已领取的门禁进程仍须停止确认，之后才允许新任务。
lease 过期撤销推进权并标记待恢复，绝不自动重发训练。Worker 重启持锁后检查并终止原命名 Job，
确认进程树为空，再经恢复 API 收敛为失败/取消；无法确认时不领取新任务。
旧 session/claim、过期推进和迟到产物不能发布模型；仅重放回执不算再次训练。

Worker 上传四个固定成员 manifest.json、pipeline.joblib、evaluation.json、splits.json。
Service 在事务外检查完整性、大小、可信内部来源、P1 格式/依赖、冻结 spec/完整实际参数、
数据身份/索引/报告，并重放原外部 Test 评估。最终短事务重新校验 claim、取消和 Artifact，
原子创建 AVAILABLE ModelAsset、更新 Run SUCCEEDED 与 model_id；training_run_id 唯一。
取消先提交则拒绝发布，成功先提交则保持成功；重复完成返回同一模型。
部分上传不产生 Model，失败/取消后 Artifact 转清理。Joblib 仅接受可信 Worker 产物，哈希不是恶意代码扫描。

### Prediction 与 MCP 请求边界

Prediction 由当前请求持有唯一计算生命周期，并发为 1，忙时不创建排队记录。
PENDING → RUNNING → SUCCEEDED/FAILED/CANCELLED；PENDING 可因启动失败或取消直接终止。
先持久化意图和 Job 身份，再建立受监管门禁子进程；确认 RUNNING 提交后才放行计算。
未确认进程树停止不能提交失败/取消终态；阻止新预测并保留恢复记录，不引入 claim/heartbeat/lease。
Service 本地互斥锁与 Training Worker 锁分离，启动先收敛旧 Job，计算中断不自动重跑。

输入为同 scope 的 AVAILABLE ModelAsset 与 DatasetAsset，恰含所有特征，允许重排。
限制 2 MiB 原始 CSV、1,000 行、100,000 单元格；只使用已拟合 Pipeline transform/predict。
模型已知特征单位要求 Dataset 显式完全相同；模型未知单位无论输入是否声明都标记 UNVERIFIED。
目标单位来自模型，不从列名推断，不换算。P1 Engine 本身的接口与包格式保持不变。

子进程只接收受控临时文件和环境白名单，创建至退出最长 30 秒，超时为 PREDICTION_TIMEOUT；
请求内独立期限监管不依赖数据库调用返回，取消信号或期限到达时仍能停止进程树。
存储 IO 另有有界超时。固定 predictions.json 包含版本、原始零基位置、预测值、目标/单位和模型/输入身份。
退出确认后验证结果、持久化 Artifact 意图、事务外上传，再原子发布 AVAILABLE Artifact、SUCCEEDED Prediction 和引用。
上传或提交结果不明保留原操作；维护恢复发布，取消/失败对象保留清理墓碑，迟到对象不能复活资源。
迁移 0002 增量增加 Prediction 和 Artifact 所有者/同 scope 约束，不改写 0001。

Prediction request digest 覆盖版本/operation/scope、模型及 manifest 摘要、输入 Dataset 原文件/表内容身份、
行数/解析/schema、特征顺序、双方单位快照及预测合同。Resource 与 MCP 为同一本地资源调用者提供不同 audience，
但 Application 将它们映射到既有 resource 幂等域，跨入口同键重放返回原资源；Worker 仍使用独立 worker 域。

MCP 默认关闭，固定 SDK 1.30.0、sse-starlette 3.0.3，使用 Streamable HTTP JSON 响应、协议 2025-11-25。
四个 Tool 为 analyze_tabular_dataset、train_tabular_regression、get_training_run、predict_with_model。
Tool 输入复用服务请求合同；scope 不在参数中，由每次已认证 HTTP 的 X-ML-Scope-ID 生成不可变上下文。
SDK session 仅承载协议生命周期，可跨 scope 顺序/并发调用；没有永久 scope 绑定或隐式默认 scope。
取消按 session/request 身份找到原调用，并核验原 scope；不能借另一 scope 取消目标。
训练/预测的幂等键来自调用者提供的 _meta["materials-ml/idempotency-key"]，不使用 MCP request/session ID。

预测的 MCP 取消或其 POST 连接中止触发领域取消，受保护清理确认进程树停止后才 CANCELLED。
同键重放不拥有原计算的取消权；无关连接结束不影响其他调用。成功先提交则保持 SUCCEEDED。
训练一旦提交 TrainingRun，MCP 取消、断连或 session 结束不取消训练，也不自动创建另一 Run。
SDK JSON transport 之外有显式 ASGI 断连桥接；此预测取消规则是服务的请求生命周期合同。

Tool 提供稳定 server/合同/schema 身份、outputSchema、structuredContent 和 JSON 文本结果。
请求和结构化结果各限 64 KiB；CSV、模型包、完整评估及预测值通过 Resource API 获取。
日志和错误不包含协议原参数、内部路径、对象 key、凭据或原始异常。
MCP 不启用 Tasks、sampling、elicitation、prompts 或文件 resources；训练提交仍是短调用。

### 平台 MCP 执行底座：已实现

仍使用单 Agent 的 Decision → Action → Observation → Decision 有界循环、统一 Registry 与 Invocation，
MCP 仅扩展 executor，不引入新的治理 profile。四个本地 ID 使用 `materials_ml_` 前缀，远端名称保持不变。
analyze_tabular_dataset / get_training_run 为 STANDARD；train_tabular_regression / predict_with_model 为 SIDE_EFFECT。
默认不注册；仅 local/test 显式配置可用。写操作使用精确 ToolRef/权限 allowlist，等待确认并在派发前重新授权；
生产 Catalog 的 Side-effect 禁令不变。远端 annotations 不授予权限，tools/list 不自动注册其他 Tool。

迁移 0017 增加 Invocation 的 binding_snapshot、remote_operation、remote_receipt；旧迁移不变。
等待确认前冻结本地 Tool 身份、executor、server_id、绑定版本、endpoint 摘要、预期 server/protocol/
remote Tool 合同和 schema hash。快照不含凭据、内部 URL 或连接对象。派发前核对漂移，旧 Invocation 不自动更新绑定。
批准的 ML Server 为 materials-ml/1.1.0、合同 materials-ml-tools-v2、协议 2025-11-25。

授权/确认后，经独立 Resource 凭据调用 `/api/v1/scopes/{scope_id}/operation-identities/prepare`，
ML Application 使用提交入口同一身份构建逻辑，返回权威 operation/digest_version/request_digest。
平台先提交这些字段和稳定键 `ml-invocation:<Invocation ID>`，再经 MCP `_meta` 注入键及 expected digest。
Service 在新资源写入前核对摘要；正式调用可能派发后不再次 prepare 或改写历史摘要。
prepare 不预留任务、不训练或预测，不是模型可见 Tool。

历史 `/operation-receipts/lookup` 只接收 operation、idempotency_key、request_digest、digest_version，
以已持久化的原幂等记录为准；不传原参数、不读取数据、不运行当前 normalizer 或 Engine。
NOT_FOUND 不是“未执行”的证明。同键冲突或暂不可查均不能触发重新派发。Resource/MCP 保留共同 resource 幂等空间。

Backend 可选依赖锁定官方 SDK mcp 1.30.0 / sse-starlette 3.0.3，不导入 ML 包、sklearn 或 joblib。
每 server 最多四个独占 session slot；scope 来自已核验归属的 Invocation.conversation_id，每次调用通过公开
HTTPX request hook 注入 X-ML-Scope-ID。session 可先后服务不同 scope，并发调用使用不同 slot。
不使用 SDK 私有接口或 monkey patch；原请求取消及 POST 清理完成后才释放 slot，异常连接关闭后重建。
Service 自身最多 32 个协议 session，空闲 300 秒过期；异常重建达到上限会受控拒绝，不转为重派发。
只读 tools/list 预检明确返回 404 且尚未发出 tools/call 时，在原 slot 和剩余预算内关闭旧连接、
重建 session 并重新校验一次；正式 Tool 请求的 404、断连或结果未知不触发该恢复，也不重派发业务。
Backend 只持有 Resource/MCP 两个不同凭据；session ID 不替代逐请求认证，不接触 Worker 凭据或 ML 数据库。

训练结果只是已提交 TrainingRun 回执。MCP 同步预测只正常返回 SUCCEEDED，FAILED/CANCELLED 为确定领域失败并保留身份。
同键 PENDING/RUNNING 重放只等待原任务终态，最多从 Tool 开始 45 秒；客户端较短预算触发原请求取消和连接清理。
Resource API 仍可查询非终态。超限、未知发布结果或无法确认进程停止不能伪造终态，重放等待者不取得原执行取消权。

结果通过固定合同、scope、资源身份、structuredContent/JSON 文本一致性检查；远端结构化结果限 64 KiB，
平台每个 JSON 对象仍限 16 KiB，不静默截断。预派发拒绝、确定领域失败和派发结果不确定分别处理；
isError=true 不代表资源没有创建。平台提交回包丢失先读取已提交本地事实，不覆盖 SUCCEEDED。

有效 MCP 绑定可以使用 OUTCOME_UNKNOWN。必须持久化 FAILED Observation，错误码 MCP_OUTCOME_UNKNOWN，
retryable=false、outcome=UNKNOWN，并说明“远端操作结果尚未确认，不能判断资源已创建或未创建，需要核查原操作回执。”
复用失败 Observation 终止路径：不再 Decision、不调用最终回答 LLM 或依赖 Tool，不提供可用资源引用，不允许普通重试。

GET `/api/v1/agent-runs/{run_id}/invocations/{id}/receipt` 与 POST 同前缀 `/reconcile` 验证平台所有权，
不占 Tool Budget。仅 Unknown 写操作可显式 reconcile；核查结果以版本 CAS 追加，不改变 Invocation 历史终态、
AgentRun、Observation 或 FinalAnswer。较晚失败查询不抹去已确认资源，迟到并发查询不能覆盖更新事实。
启动时分批收敛旧 RUNNING 为 Unknown/预派发失败，再最多核查 20 条 Unknown 并复用既有 Observation 修复。
远端 IO 不进入数据库事务，不增加后台 Worker、持续查询队列或自动恢复 Agent。

### 受控资源接入与删除协调

Backend 迁移 0018 增加 Conversation fence、ML scope binding、ResourceRef、上传 operation 和删除 operation；
ML 迁移 0003 增加中性 OPEN/CLOSED scope 与幂等关闭记录。平台只通过 Resource API 操作 ML，
不导入 ML Engine/ORM，不访问其 PostgreSQL 或 MinIO。资源代理与 MCP 注册分别默认关闭，仅 local/test 启用。

ML `/resource-identities/{type}/{id}` 返回 `ml-resource-identity-v1`、权威 `remote_identity_digest`、
不可变身份和文件完整性描述。摘要不包含可变状态；Backend 只比较摘要及版本，不重新解释审计字段。
ResourceRef 唯一约束覆盖 service/scope/type/id，仅记录曾确认的身份与来源，不缓存可用性。
每次资源查询/下载重新核查远端身份与状态；旧版本不支持、摘要冲突或服务不可用不能静默放行。

平台 `/api/v1/conversations/{id}/ml` 提供 Dataset multipart 上传、资源登记/目录、权威查询、固定成员下载，
以及 Dataset 删除和训练/预测显式取消。训练与预测创建继续走原 Agent/Registry/Invocation/MCP 治理。
`resources/{ref_id}` 是登记事实，`resources/{ref_id}/remote` 是当前权威查询，`files/{member}` 下载前验证身份、状态及文件摘要。

上传先通过 ML `/dataset-upload-identities/prepare` 取得权威摘要，再持久化稳定 key/digest 与派发记录，
正式上传携带 `X-ML-Expected-Request-Digest`。首版文件在本地传输两次，Backend 不复制 CSV/单位 normalizer。
历史 dataset.upload receipt 仅比较持久化摘要。派发未知不自动上传；确认原 PENDING Dataset 后显式同文件请求可恢复原上传。
启动和显式 reconcile 只核查原 operation，恢复引用，不创建第二份数据。上传 operation 的待核查记录参与删除 BUSY 判定。

Conversation 行上的 fence operation ID 与递增版本是持久化写入禁令。所有新工作在自身提交/派发事务中
锁定 Conversation 并检查 fence；外部调用前先持久化活动记录。删除在同一短事务中检查活动并建立 fence。
因此工作先取得推进权则删除 BUSY，fence 先提交则新 Message/AgentRun、补充/确认/重试、派发、EBSD/ML 上传及引用新增失败。
EBSD PENDING Asset 是持久化活动事实；进程内计数仅辅助。锁顺序为 Conversation → 工作/协调记录，网络 IO 不持数据库锁。

删除不会自动取消训练/预测。PENDING/RUNNING、未核实 Unknown、上传/执行/发布恢复均阻塞安全关闭。
ML 原子提交 CLOSED 与清理意图；关闭永久生效，拒绝迟到创建/启动/发布，不改变历史计算终态。
维护循环有界清理；对象墓碑继续覆盖迟到上传，身份冲突不删除归属不明对象。

平台删除 operation 为 DELETE_PENDING/RECONCILING/COMPLETED/REJECTED_BUSY。
远端不可用或回执丢失保留 Conversation、治理记录和 fence；原 operation 幂等核查 CLOSED 后才删除平台聚合。
已提交 BUSY 才能在同事务按 operation/version 解除 fence，迟到回包不能解开新的 fence。
原 DELETE 仅完成后返回 200；BUSY 为 409，待核查为 `CONVERSATION_DELETE_PENDING`。
`/api/v1/conversation-deletions/{operation_id}` 及 POST `/reconcile` 提供独立的受控查询恢复，不占 Tool Budget。
关闭只表示禁止使用并接管清理，不代表所有对象已物理删除。平台图片清理继续使用现有独立机制。

历史 MCP scope 补建只接受完整 actor/Conversation/AgentRun/Message 归属、批准 binding、
可信结果或匹配原 digest 的 receipt 及一致 scope。迁移无远端 IO；证据不足不纳管，须显式核验登记。
Unknown 即使未纳管也不能绕过 BUSY。核查不改写 Invocation/Observation/FinalAnswer，不自动恢复 Agent。

### 可信资源上下文与自然语言引用

迁移历史 0019 保留 Dataset ordinal 的引入记录，ordinal 继续不复用；0021 删除语义资源事件与 Conversation 序列。
新运行仅使用 `resource-ref-v1`。ToolDefinition 在单一 Tool Registry 注册边界中声明 ResourceParameterSpec，
ContextFramework 把当前附件和同对话资源投影为本次调用的 `r1`、`r2` 等临时引用。模型只看到名称、类型、来源、
Dataset ordinal 与受信的 schema/单位摘要，不看到平台 ID、远端 ID、摘要、存储位置、availability 或远端状态。

正常的单次 AgentDecision 同时提出工具、业务参数与 `{resource_ref: "rN"}`；无法确定或用户指向不存在的资源时提出
`{unresolved: true}`。唯一候选也不由 Backend 自动绑定，Backend 不用关键词、名称模糊匹配或最近性解释指代。
模型提案经当前 ContextFrame 的私有映射解码，ResourceContextResolver 再确定性核验 Actor/Conversation 归属、Provider、类型、
身份摘要与最终状态。只核验最终选中资源，不对全部候选执行远端扇出。

模型视图最多 20 项、16 KiB，当前消息附件优先，其余按类型公平且确定性取样。临时引用不落库、不跨调用复用。
已确认、已派发或重试使用 `resource-binding-v1` 冻结绑定，派发前复核原身份，不再让模型选择。

确定回执或经核验的模型血缘登记仍走原 ResourceRef/fence/epoch CAS。Unknown 只显式核查原操作，
不重派发、不重写 Observation/FinalAnswer、不恢复旧 Run。ResourceRef 是身份事实，不是可用性缓存。
`ENABLE_MATERIALS_ML_RESOURCE_CONTEXT` 默认关闭，仅 local/test 与 MCP/Resource 配置齐全时开启；
关闭时 Runtime 不向模型提供这些资源工具。底座直接执行的内部合同不因此更改。

### 不可变消息与独立结果观察

`useChatArtifacts` 独立于 Viewer，进入对话与本次运行完成后调用显式 `POST conversations/{id}/results/reconcile`。
只观察本对话已确认创建并登记的训练，以及结果未知的预测；每轮最多 4 项，用游标有界轮转，5 秒间隔、单窗口最多 5 分钟。
页面隐藏、切换、失败或终态停止；重新进入可补发结果。页面关闭不停止 ML Worker，平台不新增后台 Worker。
中间进度只在临时区域显示。GET result-messages 与所有 Viewer 读取不产生发布行为。

迁移 0020 给 Message 增加可空 event_key 和对话内唯一约束。外部核验/必要模型血缘登记在事务外，
确定终态与语义投影后，短事务锁同 Conversation、核对 fence/epoch、按终态事件去重并追加助手消息。
并发轮询、多标签页与丢失回包不会重复发布；原启动回答、FinalAnswer、Observation 与回执保持不变。
追加结果进入后续 Message 历史，模型能够解释真实指标而非只看到提交说明。

### 合同升级与旧业务数据

保留全部 schema migration history；没有旧 DTO、Proposal、Snapshot 或消息格式的兼容读取路径。
停止本地服务和新派发后，先运行 `scripts/dev/upgrade-chat-data.py` 输出全部不兼容聚合的目标清单、数量与阻塞原因。
可使用 `--apply --all-incompatible` 对完整预检集执行一次明确批准的批量清理，或用 `--conversation <精确 ID>` 限定集合；两者不能混用。
清理复用聚合删除、ML scope close 和受限对象清理，完成后再执行正常 Alembic upgrade head。
升级脚本不启动应用生命周期恢复，只消费本次删除返回的 cleanup ID；旧消息未清理的对话拒绝新提交。
活动工作、未知回执/上传、删除 fence 或归属不明时停止，不强删、不取消、不重派发，不清空共享卷或迁移表。
SEM、外部研究权重、配置和凭据不在清理范围。新库迁移与旧数据清理是两个独立操作。

### MCP 增量接入合同

保持现有 Decision → Action → Observation → Decision 有界循环、Registry 授权和统一 ToolDefinition。
MCP 只扩展 executor 维度，不新增治理 profile，不将 Native/Managed 强行改为 MCP，不扩展成 Multi-Agent。
四个科研意图 Tool：analyze_tabular_dataset、train_tabular_regression、get_training_run、predict_with_model。
split、scaler、CV 和指标仍是 Engine 的确定性内部流程。训练提交是短同步调用，成功结果只表示已接受
TrainingRun，不表示模型已训练好；聊天独立 POST 观察核查终态并追加结果，不运行 Agent、不占 Tool Budget。
自然语言询问进度才使用 get_training_run。

不重写 Invocation、Executor 的整体错误模型。Materials RAG 可新增 `services/materials_rag/`，复用相同
服务/引用/存储边界；现有 Runtime 目录不迁移。只有将来出现独立 Goal/State、长期上下文、并行生命周期或
权限隔离的真实需求，才另行评估 Multi-Agent。
