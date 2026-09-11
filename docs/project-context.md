# Materials Agent — Project Context

本文档保存对长期维护有价值、但不适合放进 README 或 AGENTS 的现役项目上下文：系统架构、
数据流、Tool 机制、状态模型和关键设计边界。它不是进度文件，也不声明某个分支、里程碑或
测试批次“已经完成”。当前状态必须现场检查 Git、代码、迁移、测试和运行行为。

## 文档职责与事实来源

- `README.md` 回答“项目是什么、如何运行、当前能做什么”；
- `AGENTS.md` 回答“Coding Agent 如何安全修改仓库”；
- 本文回答“系统为什么以当前边界工作，以及修改架构时必须保持哪些关系”。

发生冲突时，项目负责人当前指令优先，其次是当前代码、配置、迁移、测试和实际运行行为。
README 与本文只记录经这些来源确认的稳定事实；Git 历史只用于追溯旧设计和原因。

## 系统定位与边界

Materials Agent 是单用户、本地运行的材料研究智能体 MVP。平台主体是 Python 3.11 的 FastAPI
模块化单体；真实 ZTA35G 模型在独立 Python 3.8 Runtime 中运行，以隔离旧版 PyTorch、CUDA、
Joblib 和 scikit-learn 等依赖。

当前默认应用组合注册 `materials_unit_conversion` Standard Tool 与
`zta35g_sem_virtual_lab` Managed Tool。Backend 只有一个进程内 Tool Registry；异构执行能力
不等于动态插件系统、开放式 Agent Tool Loop 或多 Tool 编排。

```text
Frontend
  -> Backend API / application services
       -> PostgreSQL (structured facts)
       -> MinIO (generated image objects)
       -> LLM adapter (Mock or controlled DeepSeek/Qwen Provider)
       -> explicit in-process Tool Registry / Invocation control plane
            -> ExecutorRouter
                 -> in-process Standard target
                 -> Managed workflow -> Mock or real ZTA35G Runtime
```

Runtime 是模型兼容和执行隔离边界，不是把 Backend 拆成通用微服务的先例。PostgreSQL、MinIO、
LLM 和 Runtime 仍由 Backend 的应用用例编排。

## 组件职责

### Frontend

`frontend/` 提供 Vue 3 + Vite + TypeScript 的本地聊天界面。它通过 `/api/v1` 查询对话、时间线、
任务、Tool catalog、结果和资产，并通过轮询呈现异步状态。前端类型不是独立合同来源；公共
合同变化必须先核对 Backend schema 和合同测试，再同步 TypeScript 类型与组件测试。

### Backend

Backend 是模块化单体，主要分层如下：

| 层 | 职责 |
|---|---|
| `api/` | HTTP 路由、公共请求/响应 schema、依赖和错误投影 |
| `application/` | 对话与消息编排、Tool 路由/授权、执行、结果、资产和解释用例 |
| `domain/` | Task、ToolRun、ToolResult、LLMCall 等领域状态与端口合同 |
| `infrastructure/` | SQLAlchemy/PostgreSQL、MinIO、LangChain Provider/Mock LLM、Runtime client |
| `alembic/versions/` | 持久化 schema 的递增迁移 |

跨层修改必须保持公共 API、领域不变量、迁移、Repository 映射和测试一致。数据库 schema 不是
单独的产品合同；领域模型与迁移也不能各自演化。

### Runtime

`mock-runtime/` 在 Python 3.11 中实现与真实 Runtime 一致的 HTTP 合同，用于确定性开发与测试。
`zta35g-runtime/` 在 Python 3.8 中加载只读 `SEM/ZTA35G_lab` 模型包并执行真实推理。两者都
提供内部 health/ready（含受支持 Tool 元数据）和 execute 边界，Backend 通过本地 client
adapter 调用；公共 Tool catalog 由 Backend Registry 提供。

Backend 与真实 Runtime 不共享 Python 包或虚拟环境。`SEM/` 是外部研究模型包，不属于普通
源码重构范围；其完整性由 `docs/acceptance/sem-package-manifest.json` 校验。

## 主要数据流

一次自然语言 Tool 请求的统一控制路径是：

```text
用户消息
  -> 为本次路由创建 RoutingCatalogSnapshot
  -> Context Builder 从同一 Conversation 选择预算内的近期完整轮次
  -> LLM safe projection -> Structured Output 或 native Tool Calling
  -> ToolInvocationProposal(model_tool_name, proposed_arguments)
  -> Registry.resolve(original snapshot + current registration)
  -> Execution Policy / authorization
  -> InvocationRun -> ExecutorRouter
       -> Standard: 同步无副作用执行与 InvocationResult
       -> Side-effect: 确认、再次授权、幂等执行或 OUTCOME_UNKNOWN
       -> Managed: Task/InputRevision/ToolRun/Asset/Explanation 严格链路
  -> timeline/API -> Frontend
```

Catalog、LLM 和 Invocation API 分别使用字段 allowlist 的投影 DTO，禁止直接序列化完整
`ToolDefinition`。LLM 只能看到模型 Tool 名、描述和 `proposal_schema`，只提出名称与参数；
version 和 `schema_hash` 由原 Routing Snapshot 与当前 Registry 双重 resolve 后补全。Catalog
查询结果、LLM 输出、确认动作和历史策略快照都不授予执行权限。

## Conversation 级上下文记忆

Conversation Context 由应用层独立构建，Provider adapter 只接收已构建的消息序列和 Structured
Output 合同，不查询数据库，也不负责参数合并、引用解析或 Tool 授权。历史选择以当前消息的
`(created_at, message_id)` 为排他上界，从最近的完整 user/assistant 轮次反向选取连续后缀，
不拆分单轮；首次路由限定在同一 Conversation，已绑定 Task 的补参限定在当前 Task，并附带
结构化 Agent State。接口预留 `summary_segments + recent_turns + agent_state`，当前不生成滚动
摘要。

预算使用 `cl100k_base` 做确定性近似。每个具体模型必须声明自己的 `context_window_tokens`；
角色分别配置应用 Prompt 上限、历史预算、最大输出和 safety margin。`1,024` 的默认 safety
margin 是可调整的工程保护值，不保证覆盖所有 Provider tokenizer 差异。Context Builder 在
选择历史后对最终消息重新计数；如果零历史时必需 Prompt 仍超限，不调用 Provider，并以
`CONTEXT_BUDGET_EXCEEDED` 结束首次 LLMCall 且不创建 Task，或让补参 Task 保持
`NEEDS_INPUT`。

NEW_TASK 不隐式继承任何历史实验参数。模型只有在当前消息明确引用历史条件时，才能返回本次
Context Window 内的局部 `context_ref` 和当前消息中的原文引用。应用对两段文本做 Unicode
NFKC 与空白折叠后执行大小写敏感的精确包含检查，再把局部引用解析为同一 Conversation、同一
Tool 的 Task/InputRevision 结构化事实。有效引用以历史 normalized input 为 base，按 Tool
顶层字段合并当前 delta；嵌套参数整体替换。无引用时 base 为空。两条路径最终都经过 normalizer、
当前执行 schema 的一致性门禁和 Registry `NEW_BINDING` 授权；无效引用进入协议失败，不猜测
替代引用。

历史中的 Task/InputRevision、ToolRun、ToolResult 与成功 Explanation 只形成受控 assistant
投影。ToolResult 不默认复制完整 `data`；Tool 注册可提供带版本的安全 Context Projection；没有
定制投影时只注入状态与输出类型等 metadata。历史轮次经带类型边界的 canonical JSON 安全序列化；
历史消息、引用文本和投影结果在 Prompt 中均被标记为不可信数据，不能修改系统规则、Agent
State 或 Tool 授权。

## Tool Registry 与扩展模型

Registry 是显式、不可变、进程内的唯一注册边界。`ToolDefinition` 只保存不可变、可序列化的
元数据：标识与版本、生命周期、执行 profile/mode、`executor_id`、输入/提议/输出 schema、
ToolExecutionPolicy、展示模式和安全说明。normalizer、validator、execution target、codec、
presenter、preview 与 health probe 位于独立 `ToolExecutionBinding`；两者组成 `RegisteredTool`。
Binding 不持有 Executor，无状态 Executor 由 `ExecutorRouter` 按 `executor_id` 管理。

Registry 启动时联合校验 Definition、Binding 与 ExecutorRouter，拒绝重复 ID、非法生命周期、
不匹配的 profile/binding、缺失 Presenter、不安全 schema 和 async-only Tool。LangChain Adapter
可把 `@tool`、`StructuredTool` 或第三方 `BaseTool` 转成同一 RegisteredTool；LangChain Tool
实例只作为 execution target，不成为领域核心抽象。当前没有独立 LangChain Registry、包扫描、
动态上传、HTTP/MCP Executor 实现或开放式自动 Tool Loop。

新增 Tool 的预期扩展路径是：

1. 定义纯元数据 ToolDefinition，并提供对应 ToolExecutionBinding；
2. 选择已注册 Executor；Managed 模型继续提供隔离 Runtime 与完整领域链路；
3. 在应用组合根显式注册到唯一 ToolRegistry；
4. 补齐投影、授权、执行、持久化和端到端测试。

测试中的 `ml_training_test` 是异构合同夹具，只证明 Registry/Router 的扩展能力，不是产品 Tool。

## 路由、规范化与 Task Binding

首次路由结果遵循以下规则：

| 受控路由与规范化结果 | Task Binding | Task 状态 |
|---|---|---|
| 唯一候选且参数完整 | 在 resolve 与 normalize 成功后绑定 | `READY` |
| 唯一候选但缺参或存在参数歧义 | 绑定该唯一 Tool | `NEEDS_INPUT` |
| 多个有效候选 | 不绑定；输入 Revision 保存受控候选引用 | `NEEDS_INPUT` |

Binding 保存首次绑定的 `tool_id`、`version` 和 `schema_hash`。三者必须同时存在，并且一旦写入
就不能在同一 Task 中切换或改写，即使只是版本变化也不能修改原 binding。

已绑定的 `NEEDS_INPUT` Task 收到补充内容后，不重新调用首次路由 LLM，也不重新选择 Tool。
固定 Tool 的参数提取器只产生参数增量，再由已绑定 Tool 的 normalizer 与上一输入 Revision
合并。补参、执行和重试都必须再次由当前 Registry 授权。

## `schema_hash` 与版本兼容门禁

`schema_hash` 对影响执行语义的 Tool `input_schema` 计算 SHA-256。Canonical JSON 使用字段
排序、UTF-8、紧凑编码并拒绝 NaN、Infinity、非文本键和其他非标准 JSON 值。展示名、描述、
`proposal_schema`、`output_schema`、UI 字段、Runtime 地址和执行对象不参与该 Hash。

Hash 只证明执行输入合同相等，不推断向前或向后兼容：

- 当前注册项 Hash 与 Task binding 不同：拒绝补参、执行和重试；旧 Task 不自动迁移；
- 版本变化但 Hash 相同：已绑定 Task 可以继续，由新的 ToolRun 记录执行时的当前版本；
- 原 Task binding 保持不变，不能用兼容版本覆盖历史绑定事实。

## Tool 生命周期与执行授权

生命周期和执行策略是两个字段，但当前 Registry 只接受三种组合：

| `status` | `lifecycle_policy` | 新路由 | 已绑定 Task 的补参/执行/重试 |
|---|---|---|---|
| `ACTIVE` | `ANY_TASK` | 允许 | 允许 |
| `DEPRECATED` | `EXISTING_TASK_ONLY` | 排除 | 合同未漂移时允许 |
| `DISABLED` | `NONE` | 排除 | 拒绝 |

Registry 启动时拒绝 `ACTIVE + NONE`、`DISABLED + ANY_TASK` 等非法组合。退役不会物理删除 Tool
或历史数据；旧 `ExecutionPolicy` 名称保留为 `ToolLifecyclePolicy` 的兼容 alias。

## InvocationRun 状态、确认与恢复

Standard/Side-effect 不创建 Managed Task；Message 与 LLMCall 的 `task_id` 因此可空，LLMCall
直接关联 source Message。Managed Invocation 必须关联 Task 与同 Task 的 ToolRun，Standard 与
Side-effect 则通过一对一强 FK 关联 InvocationResult。消息提议使用部分唯一约束，保证一条用户
消息最多产生一个自动执行 Invocation；native Provider 返回多个 tool calls 时整批拒绝。

InvocationRun 的唯一权威状态转换为：

| 当前状态 | 允许后继 |
|---|---|
| `PENDING` | `PENDING_CONFIRMATION`、`RUNNING`、`DENIED`、`FAILED` |
| `PENDING_CONFIRMATION` | `PENDING`、`REJECTED`、`EXPIRED` |
| `RUNNING` | `SUCCEEDED`、`FAILED`、`OUTCOME_UNKNOWN` |
| 所有终态 | 无 |

只有 Side-effect 使用确认，事实由 confirmed/rejected/expired 时间与 actor 字段记录，不另建
Confirmation 状态。执行先提交 claim/lease，再单独提交 dispatch marker，所有 target 调用都在
marker 提交以后且位于事务外。重复确认可继续驱动已确认的 PENDING；未过期 RUNNING 不重复调用。
lease 过期时，无 marker 可重新授权接管；有 marker 的 Standard 可用同一幂等键重放，Side-effect
转 `OUTCOME_UNKNOWN`，Managed 只与 ToolRun/ToolResult 对账并要求显式重试，绝不再次调用 Runtime。

## Task 与 ToolRun 状态模型

Task 表达整个科研任务的业务阶段：

| Task 状态 | 含义 |
|---|---|
| `PENDING` | Task 已创建，尚未形成可执行的受控路由状态 |
| `NEEDS_INPUT` | 唯一 Tool 已绑定但仍缺参/有歧义，或多个有效候选需要澄清 |
| `READY` | 唯一 Tool 已绑定，最新输入 Revision 完整，可申请执行授权 |
| `RUNNING` | 当前一次尝试已经进入执行流程 |
| `SUCCEEDED` | 请求输出全部成功 |
| `PARTIALLY_SUCCEEDED` | 至少一个请求输出成功、至少一个失败 |
| `FAILED` | 任务以受控失败终结 |

`READY` 必须有完整 binding 和完整最新输入 Revision。已绑定的 `NEEDS_INPUT` 必须真实存在缺失
或歧义字段；未绑定的 `NEEDS_INPUT` 必须保存至少两个受控候选引用。

ToolRun 只表达一次模型尝试：

```text
PENDING -> RUNNING -> SUCCEEDED
                   -> PARTIALLY_SUCCEEDED
                   -> FAILED
```

`PENDING` 已保存授权和执行快照但没有执行结果；`RUNNING` 已持久化开始时间；终态必须包含
完整计时，并以 completed/failed output 集合恰好覆盖 requested outputs。ToolRun 不使用
`NEEDS_INPUT` 或 `READY`。

## 执行、事务与重试

Managed 执行在 `authorize()` 通过后继续沿用严格链路：

1. 在短事务内创建并提交 `PENDING` ToolRun，同时把 Task 置为 `RUNNING`；
2. 在另一短事务内把 ToolRun 置为 `RUNNING` 并保存开始时间；
3. 在数据库事务外调用 Runtime；
4. Runtime 返回后，在新事务中提交结果和终态；图片对象通过受控资产流程进入 MinIO；
5. 结果提交失败时不能伪造内存成功，也不能覆盖数据库中的真实状态。

外部 LLM、Runtime 和 MinIO 调用不得放进数据库长事务。Backend 不自动重试 Runtime execute。
显式重试会重新读取当前 Registry、重新授权，并创建新的 ToolRun、递增 attempt 和新的 seed；
旧 ToolRun、旧结果和旧资产保持不可变。

每个 ToolRun 只消费并产出自己的执行快照、图片、性能结果和诊断。结果提交必须核对 Task、
ToolRun、输入 Revision、资产所有者和来源一致性，不能跨 actor、跨 Task 或跨 ToolRun 拼接。

## Conversation 写入恢复与永久删除

Conversation 创建和 Message 提交分别使用持久化幂等记录。空白工作区的首条消息由一个
客户端 operation descriptor 固定派生两个 key；刷新或结果不确定后的重试继续使用同一组 key，
不通过 Timeline 推测写入结果。Message 已提交但编排尚未越过可靠启动边界时，重放继续原
LLMCall；路由确定前 Message/LLMCall 的 `task_id` 保持为空；
已有终态或 ToolRun、Result、Assistant Message 等真实执行事实时只返回现有投影，不重复创建
Message/Task 或执行链。

Invocation 查询、Timeline 刷新、消息幂等重放与重复 confirm 都会先执行同一按需恢复逻辑，不
引入后台 Worker。进程启动在接受请求前恢复其他旧进程遗留状态：可靠启动前的 PENDING LLMCall
以中断原因结束，原
Task 保持可继续；旧 RUNNING LLMCall、ToolRun 和 Explanation 以进程中断失败，无法安全继续的
Task 进入终态；PENDING Asset 只在数据库中标记 ORPHANED，不在恢复事务中访问 MinIO。删除时
还会对目标 Conversation 使用同一进程 cutoff 再执行恢复，因此旧 PENDING/READY 不会永久造成
`CONVERSATION_BUSY`。

永久删除以 PostgreSQL 短事务为业务成功边界。事务锁定 owner、Conversation、Task 与相关执行
记录，拒绝当前进程仍真实活动的聚合，从真实 Asset 行生成不可变 cleanup 快照，再级联删除
Conversation 聚合；cleanup 写入失败会回滚整个事务。MinIO 调用只发生在事务提交和 HTTP 响应
之后，失败不会恢复 Conversation。

cleanup 只接受严格的系统对象路径，且路径中的 `asset_id` 必须与事务快照一致；bucket 与 storage
namespace 在 Asset 创建或历史删除快照时固化。新 Asset 使用 `METADATA_V1`，上传后必须回读并
核对 `asset-id`、`operation-id` 和 producer ToolRun；升级前的 `LEGACY_DB_KEY` 在三项身份 metadata
完全不存在时可依赖数据库身份删除，部分存在或任何不匹配都进入非自动重试的 `SAFETY_BLOCKED`。
自动 drain 只处理最旧的 `PENDING`，每次最多 100 条；触发点是应用启动、删除响应后的 best-effort
处理和后续删除顺带重试，另保留一次性维护命令，不引入常驻 Worker。

## LLM Provider 与角色配置

Backend 只在 `LLM_ADAPTER=provider` 时惰性导入 LangChain Provider 模块。启动时读取固定的
`backend/config/llm.toml`，分别为首次聊天路由、已绑定 Tool 补参和 ToolResult 解释构造独立
ChatModel。DeepSeek 使用 `ChatDeepSeek`；Qwen 使用 `ChatOpenAI` 连接固定的阿里云百炼国内
OpenAI-compatible endpoint。两者均关闭 SDK 自动重试，也不做 Provider fallback。

TOML 中的模型能力声明是开发期约束，不是动态发现结果。它可以收紧模型允许的参数、推理模式
和结构化输出组合，但不能放宽代码中的 Provider 硬边界。三个角色继承全局默认模型，也可分别
覆盖模型和 generation 参数；省略的可选参数不发送给 Provider。配置仅在 Backend 重启时重载，
不提供请求级切换、管理 API 或前端配置页。

三类 Prompt 由版本化 `ChatPromptTemplate` 统一渲染。首次路由可配置 Structured Output/JSON
fallback 或 native `bind_tools/tool_calls`；两者都只生成调用建议，不直接触发 Runtime，也不
启动 AgentExecutor。一次调用使用的同一份消息同时参与
Prompt digest，避免审计摘要和实际请求分叉。Provider 返回的 `reasoning_content`、完整原始
响应和完整 Prompt 不进入日志、数据库或公共响应。

## 执行快照与审计

ToolRun 保存执行时的：

- `tool_id`、当前 `version`、`schema_hash`；
- `normalized_input_snapshot` 与实际 `execution_input`；
- `execution_policy_snapshot`；
- 输入 Revision、attempt、requested outputs 和 seed；
- Runtime 返回的受控参数、输出摘要、诊断和模型 bundle 标识。

LLMCall 审计保存实际 provider/model、安全 Catalog 引用与 Hash 或固定 Tool 上下文、受控
Structured Output 摘要、Prompt digest，以及带 schema version 的有效 generation 参数。领域层
仍可读取旧版 `candidate_input` 摘要；新的首次路由保存 `proposed_arguments`，只有已绑定 Managed
Task 的补参提取保存 `candidate_input_delta` 与可选局部历史引用。
nullable `context_snapshot` 使用严格的 ContextSnapshot v1 应用层 schema，记录策略、模型、预算、
token 统计、选中来源、局部引用映射和 digest，但不保存完整 Prompt。Snapshot 的 canonical JSON
超过 128 KiB 时只聚合审计元数据，保留首尾来源、类型计数和完整有序来源集合 digest；该聚合
不会改变已选择的历史、实际 Prompt 或 Provider 请求。它不保存 Provider 原始响应或推理内容。

审计数据用于复现“当时根据什么受控事实做出决定”，不授予后续执行权限。重试与补参始终以
当前 Registry 再授权。

## ZTA35G Runtime 合同

当前 ZTA35G Managed Tool 只接受 ZTA35G 材料与受控热处理参数，能够生成 SEM，并按 requested outputs 返回
力学性能。即使只请求力学性能，Runtime 仍会生成中间 SEM。

固定推理参数属于 Backend、Mock Runtime、真实 Runtime 和测试共同维护的合同：

```text
num_samples=1
guide_scale=2.0
timesteps=1000
```

`seed` 由每次 ToolRun 决定。以上固定参数不得改成普通环境变量；任何变化必须同步合同、实现、
Mock、真实 Runtime 和测试。

Runtime token 每次本地启动生成，只在内存中注入 Backend 与 Runtime 子进程。真实 Runtime 的
模型根必须指向只读外部包；不得把权重路径、内部绝对路径、Tensor 或图片 bytes 写入日志或
公共响应。

## 数据与安全边界

- PostgreSQL 是结构化业务事实的存储；MinIO 是生成图片对象的存储。
- 日志和公共响应不保存图片 bytes、完整 Prompt、完整 Provider 响应、Tensor、Secret、权重
  路径或内部绝对路径。
- `.env`、数据库、对象存储数据、生成输出、模型权重、缓存、日志、`tmp/` 和机器状态不进入
  Git。
- 公共错误只暴露受控、有限的错误码和安全消息；内部异常细节不能穿透 API 或 Runtime 边界。
- `SEM/` 只读且不进入普通 Git 跟踪；完整性检查只枚举文件、大小和 SHA-256，不导入模型。

## 当前非目标与范围变化

当前设计不包含 Redis、后台 Worker、SSE、WebSocket、登录、多用户隔离、真实 SEM 上传、
EBSD 输入、ML Training、Planner、多 Agent、动态插件上传或生产部署。

Conversation 记忆当前也不包含跨 Conversation 共享、滚动摘要、用户级记忆控制或额外模型
判断；历史引用只在单次 Context Window 内使用局部引用，不属于公共 API。

以下变化不是局部实现细节，必须先确认产品/架构范围：

- 引入动态 Tool 发现、上传、卸载、生产 Side-effect Tool 或自动多 Tool 编排；
- 引入 Worker、消息队列、Redis、流式推送或跨进程调度；
- 引入登录、多用户权限或新的数据隔离模型；
- 允许用户上传真实 SEM/EBSD 或其他文件；
- 合并 Backend 与真实 Runtime 环境，或改变 `SEM/` 的只读外部包地位；
- 改变固定推理参数、重试语义、Tool binding 不可变性或 `schema_hash` 门禁；
- 生产部署、远程 Runtime 或任何超出本地回环网络的运行方式。

实施已确认的范围变化时，必须同步代码、配置、迁移、合同测试、README 和本文，确保下一次
会话不会从旧边界出发。

## 维护时的验证地图

| 变化主题 | 首要证据与测试位置 |
|---|---|
| Tool 定义、Hash、生命周期、授权 | `backend/src/materialsagent/application/tool_registry.py`、`backend/src/materialsagent/domain/ports/tool_registry.py`、`backend/tests/unit/test_tool_registry.py` |
| 路由、补参和 binding | `backend/src/materialsagent/application/chat_orchestration.py`、Task/Input Revision 模型、相关 unit/contract/api 测试 |
| Task/ToolRun 状态与重试 | `backend/src/materialsagent/domain/`、tool execution/workflow、unit 与 integration DB 测试 |
| 持久化字段 | `backend/alembic/versions/`、SQLAlchemy 映射、migration/repository integration 测试 |
| Runtime HTTP 合同 | Backend contract 测试、`mock-runtime/tests/`、真实 Runtime contract 测试 |
| 真实模型兼容性 | `zta35g-runtime/tests/compatibility/`；只有明确需要时加载模型或 GPU |
| 前端公共合同 | Backend schema/contract、`frontend/src/api/`、前端组件与流程测试 |
| 本地启动与资源 ownership | `scripts/dev/local-dev.ps1` 与 `scripts/dev/test-local-dev.ps1` |

具体命令和最小验证要求以 `README.md` 与 `AGENTS.md` 为准；本文不复制一套会漂移的操作手册。
