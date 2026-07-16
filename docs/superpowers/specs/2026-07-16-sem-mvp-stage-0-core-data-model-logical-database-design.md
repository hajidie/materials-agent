# 材料智能体平台阶段 0 第三节：核心数据模型与数据库逻辑关系设计

> 状态：已确认设计基线
>
> 确认日期：2026-07-16
>
> 前置基线：第一节与第二节均为“已确认设计基线”
>
> 适用范围：阶段 0 逻辑数据模型设计
>
> 事实依据：第一节与第二节均为已确认设计基线；本轮重新检查仓库状态并完整复核第三节，只修订第三节，未修改或运行 SEM/，未加载模型或权重。

## 1. 范围与非范围

### 1.1 本节目标

本节把第一节的核心实体和第二节的持久化顺序落成逻辑数据模型，回答：

- 哪些业务事实需要进入 PostgreSQL；
- Actor、Conversation、Message、Task、ToolRun、Asset、ToolResult、Explanation 和 LLMCall 如何关联；
- 哪些字段必须使用普通列、外键和约束，哪些 Tool 特有内容适合 JSONB；
- 如何保证 selected ToolRun/Result 一致且不同执行尝试的产物不混合；
- 幂等、NEEDS_INPUT、资产检查点、结果提交和解释重试分别落到哪些实体；
- 哪些短数据库事务必须原子提交；
- 未来增加 Tool、SSE 和登录系统时，哪些关系可以继续复用。

本文是逻辑设计，不给出 SQLAlchemy、PostgreSQL DDL 或 Alembic 实现。

### 1.2 本节非范围

本轮不设计或实现：

- SQLAlchemy Model、Pydantic Schema、建表 SQL、Alembic migration 或 Repository；
- FastAPI 路径、HTTP 请求/响应 Schema 或错误响应协议；
- 本地 ZTA35G Tool Runtime 的 HTTP/RPC 路径、序列化协议、超时或进程管理；
- Tool、Runtime、Client Adapter、StorageService、LangChain 或前端代码；
- Docker Compose、Conda 环境、依赖安装、模型加载、权重运行或 SEM/ 修改；
- Event/Event Store、状态转换历史、完整 Observability 表或结构化日志入库；
- 阶段 1 实施计划或阶段 1A/1B 实施工作；
- git commit。

公共 API 路径和本地 Runtime 协议留到阶段 0 第四节。

### 1.3 本节结论

采用“强关系核心 + 有边界的 JSONB”：

1. 标识、普通外键、状态、版本、时间、错误码、来源、选择关系和资产文件元数据使用普通列。
2. Tool 特有输入输出、运行参数、diagnostics、provenance 和安全 LLM structured output summary 使用受控 JSONB。
3. requested/completed/failed outputs 已确认使用 PostgreSQL text array，不放进任意 JSONB。
4. ToolResult 的 artifacts 已确认使用 ResultAssetLink 关联 Asset，不把 asset_id 列表藏在 JSONB。
5. 每个 ToolRun 最多产生一个 ToolResult；一个 Task 可以有多个不可变 ToolRun。
6. Task 的 selected_tool_run_id 与 selected_result_id 使用少量高价值复合约束保证来自同一 Task 和同一 ToolRun。
7. Asset 归属于 Actor/Task；producer_tool_run_id 仅表示生成来源并允许为空。当前 MVP 只创建 GENERATED Asset，未来 UPLOADED Asset 不需要伪造 ToolRun。
8. ToolRun 只引用 task_input_revision_id，并保存实际发送给 MaterialTool.execute 的 execution_input，不重复保存 raw/normalized input。
9. 自然语言意图与参数提取使用 LLMCall(INTENT_AND_PARAMETER_EXTRACTION) 记录；TaskInputRevision 仍是 Application 处理后的输入事实。
10. Message、TaskInputRevision、ToolRun 和 LLMCall 保存 request_id 关联，但不建立 Request 表或新运行层级。
11. Task 不保存 duration_ms；总墙钟时间由时间戳计算，实际耗时保存在 ToolRun/LLMCall/Explanation。
12. Explanation 重试追加新的 NaturalLanguageExplanation 和 LLMCall，继续引用同一 ToolResult。
13. TaskProgressReporter、结构化日志和 Tool Registry 均不进入本节业务表。

## 2. 数据建模原则

### 2.1 事实、投影与临时数据

PostgreSQL 中的核心事实是：

- 稳定主体与所有权；
- 对话、不可变消息和稳定 Task；
- 每次输入快照与幂等绑定；
- 每次实际 Tool 执行尝试；
- 文件元数据与跨存储状态；
- 结构化 ToolResult；
- 意图/参数提取、知识回答、Explanation 三类 LLM 调用尝试。

下列内容只是投影或临时数据，不建设独立业务实体：

- Tool Catalog：从代码级 Tool Registry 生成的只读投影；
- TaskProgressReporter 进度：MVP 不持久化；
- 结构化日志：进入日志系统，不进入业务表；
- Tool 内部步骤：进入 ToolRun.diagnostics，不建立 StageRun；
- Runtime 内存对象、图片 bytes 和模型 Tensor：不进入 PostgreSQL；
- model bundle：静态配置或兼容性记录，不建立模型版本表。

### 2.2 三种建模方式比较

| 方案 | 优点 | 主要问题 | 结论 |
|---|---|---|---|
| 所有内容放入大 JSONB | 初期字段少 | 外键、唯一性、排序、状态筛选和所有权难以约束 | 拒绝 |
| 将每个输入、输出、warning 和 diagnostic 完全拆表 | 关系最细 | 表数量和迁移成本过高，且把 Tool 内部差异固化进平台 | 拒绝 |
| 关系型核心列 + 受控 JSONB + 两个轻量关联实体 | 核心完整性可约束，Tool Schema 可演进 | 需要明确 JSONB 边界和应用层 Schema 校验 | 采用 |

两个轻量关联实体是：

- TaskInputRevisionMessage：记录一个输入快照依据了哪些不可变消息；
- ResultAssetLink：以外键表达 ToolResult 的 artifacts，并强制 Asset 与 Result 来源一致。

它们不是运行层级、事件或状态机。未来上传文件作为 Tool 输入时可以再增加轻量输入资产关联，但当前不创建 Upload 实体或 ToolRunInputAssetLink。

### 2.3 不可变与可变边界

- UserMessage 和已保存的 AssistantMessage 不覆盖，只追加。
- TaskInputRevision 不覆盖，只按 revision 追加完整快照。
- 终态 ToolRun、ToolResult、ResultAssetLink 和 Explanation 尝试不覆盖业务内容。
- 显式 Tool 重试创建新 ToolRun；解释重试创建新 Explanation 和 LLMCall。
- Task 允许更新 current_status、created/started/updated/completed 时间、错误和 selected 引用；不保存含义不清的聚合 duration_ms。
- Asset 只沿 PENDING / AVAILABLE / FAILED / ORPHANED 生命周期更新。
- Conversation 只允许更新展示性标题、updated_at 等会话元数据，不修改历史消息。

### 2.4 删除行为

MVP 核心事实默认不使用级联硬删除：

- Actor、Conversation、Task、ToolRun、Asset、ToolResult 被引用时使用 RESTRICT 语义；
- 对话删除、账户注销、数据保留和对象清理规则留给正式登录与合规设计；
- MinIO 对象删除不能通过数据库级 cascade 触发；
- ORPHANED 是跨存储可恢复状态，不等于数据库行已删除。

## 3. 逻辑 ER 图

### 3.1 核心 ER 图

~~~mermaid
erDiagram
    ACTOR ||--o{ CONVERSATION : owns
    ACTOR ||--o{ TASK : owns
    ACTOR ||--o{ ASSET : owns
    ACTOR ||--o{ TOOL_RESULT : owns

    CONVERSATION ||--o{ MESSAGE : contains
    CONVERSATION ||--o{ TASK : contains
    TASK ||--o{ MESSAGE : groups

    TASK ||--o{ TASK_INPUT_REVISION : snapshots
    TASK_INPUT_REVISION ||--|{ TASK_INPUT_REVISION_MESSAGE : cites
    MESSAGE ||--o{ TASK_INPUT_REVISION_MESSAGE : contributes
    LLM_CALL o|--o{ TASK_INPUT_REVISION : may_extract

    TASK ||--o{ IDEMPOTENCY_RECORD : protects
    TASK ||--o{ TOOL_RUN : attempts
    TASK o|--o| TOOL_RUN : selects
    TASK o|--o| TOOL_RESULT : selects

    TASK ||--o{ ASSET : owns
    TOOL_RUN o|--o{ ASSET : may_produce
    TOOL_RUN ||--o| TOOL_RESULT : produces

    TOOL_RESULT ||--o{ RESULT_ASSET_LINK : exposes
    ASSET ||--o| RESULT_ASSET_LINK : referenced_by

    TOOL_RESULT ||--o{ NATURAL_LANGUAGE_EXPLANATION : explained_by
    NATURAL_LANGUAGE_EXPLANATION ||--|| LLM_CALL : generated_by

    TASK ||--o{ LLM_CALL : invokes
    LLM_CALL ||--o| MESSAGE : may_generate
~~~

图中的两个 selects 关系不是新的实体：

- Task.selected_tool_run_id 是可空外键；
- Task.selected_result_id 是可空外键；
- 二者通过复合外键共同指向同一 Task 下同一 ToolRun 产生的 ToolResult。

Asset 对 ToolRun 是可选来源关系：每个 Asset 必须属于一个 Actor 和 Task，但 producer_tool_run_id 可以为空；GENERATED Asset 必须有 producer ToolRun，未来 UPLOADED Asset 必须没有 producer ToolRun。

### 3.2 普通知识问答路径

~~~mermaid
flowchart LR
    A["Actor"] --> C["Conversation"]
    C --> U["User Message"]
    C --> T["Task"]
    U --> I["LLMCall: INTENT_AND_PARAMETER_EXTRACTION"]
    I -->|"判定为知识问答"| L["LLMCall: KNOWLEDGE_ANSWER"]
    L --> M["Assistant Message"]
    T -. "不创建" .-> X["ToolRun / Asset / ToolResult"]
~~~

自然语言知识问答先保存意图提取 LLMCall 及安全 structured_output_summary，再执行 KNOWLEDGE_ANSWER 调用。知识回答成功时保存 AssistantMessage；整个路径不创建 ToolRun、Asset 或 ToolResult。任一 LLM 失败时保存失败 LLMCall 和 Task 错误，不创建伪造的 AssistantMessage。

### 3.3 Tool 路径

~~~mermaid
flowchart TD
    M["Message"] --> I["LLM structured output / optional"]
    I --> R1["TaskInputRevision.raw_input"]
    R1 --> R2["TaskInputRevision.normalized_input"]
    R2 --> E["ToolRun.execution_input"]
    E --> TR1["ToolRun attempt 1"]
    TR1 --> MT["MaterialTool.execute"]
    T["Task"] --> R1
    T --> TR1
    T --> TR2["ToolRun attempt 2 / retry"]
    TR1 --> A1["Asset(s) from attempt 1"]
    TR1 --> O1["ToolResult from attempt 1"]
    TR2 --> A2["Asset(s) from attempt 2"]
    TR2 --> O2["ToolResult from attempt 2"]
    O2 --> E1["Explanation attempt 1"]
    O2 --> E2["Explanation attempt 2"]
    T --> S["selected_tool_run_id + selected_result_id"]
    S --> TR2
    S --> O2
~~~

直接结构化请求或完全确定性的补充可以跳过 I，使 TaskInputRevision.source_llm_call_id 为空。任何 ResultAssetLink 都只能连接同一个 task_id 下、producer_tool_run_id 等于 Result.tool_run_id 的 AVAILABLE Asset。旧尝试的图片不能与新尝试的性能结果组合。

## 4. 实体逐项定义

### 4.1 Actor

用途：保存稳定所有权主体，为 MVP 匿名主体和未来登录关联提供同一个 actor_id 锚点；不表示角色、租户或计费账户。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| actor_id | 是 | opaque id | 主键；MVP 稳定本地匿名主体 |
| user_id | 否 | opaque id/text | 未来认证主体；MVP 为空 |
| actor_origin | 是 | short text | MVP 为 LOCAL_ANONYMOUS；只表示来源，不是角色 |
| created_at | 是 | timestamp | Actor 建立时间 |
| linked_at | 否 | timestamp | 未来安全关联 user_id 的时间 |

- 主键：actor_id。
- 外键：MVP 不建立 User 表，因此 user_id 暂不设业务外键。
- 唯一约束：actor_id。已确认一个未来 user_id 可以关联多个 actor_id，因此 Actor.user_id 不设唯一约束。
- 状态字段：无。Actor 不是认证状态机。
- 时间字段：created_at、linked_at。
- 错误字段：无。
- JSONB：无。
- 不保存：密码、令牌、角色矩阵、tenant_id、计费信息或认证会话。

### 4.2 Conversation

用途：对消息和 Task 提供稳定会话边界，并关联所有者。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| conversation_id | 是 | opaque id | 主键 |
| actor_id | 是 | opaque id | 外键到 Actor |
| title | 否 | text | 展示性标题，不作为业务判定依据 |
| created_at | 是 | timestamp | 创建时间 |
| updated_at | 是 | timestamp | 最后一次会话元数据或消息追加时间 |

- 主键：conversation_id。
- 外键：actor_id → Actor.actor_id。
- 唯一约束：conversation_id。actor_id 使用普通外键；下游重复所有权由 Application 短事务校验，不强制建立所有权复合候选键。
- 状态字段：无。
- 时间字段：created_at、updated_at。
- 错误字段：无。
- JSONB：MVP 不需要通用 metadata JSONB。
- 不保存：消息数组、Task 数组、完整 Prompt、登录凭据或派生执行状态。

### 4.3 Message

采用统一 Message 实体，通过 role 区分 UserMessage 与 AssistantMessage。统一表更适合按会话时间排序，也避免两张表重复实现所有权和不可变规则。

用途：保存对话中已提交的用户文本、助手知识回答和追问文本。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| message_id | 是 | opaque id | 主键 |
| conversation_id | 是 | opaque id | 所属 Conversation |
| task_id | 是 | opaque id | 本消息创建或恢复的稳定 Task |
| actor_id | 是 | opaque id | 资源所有者；助手消息仍归该 Actor 所有 |
| request_id | 是 | opaque correlation id | 创建该消息的请求；无 Request 表或外键 |
| role | 是 | short text | USER 或 ASSISTANT |
| generation_source | 是 | short text | USER、LLM 或 TEMPLATE |
| content_text | 是 | text | 已持久化可显示文本 |
| structured_content | 否 | JSONB | 仅用于未来受控结构化展示片段 |
| llm_call_id | 否 | opaque id | LLM 生成的 AssistantMessage 关联 LLMCall |
| created_at | 是 | timestamp | 不可变消息创建时间 |

- 主键：message_id。
- 外键：
  - conversation_id → Conversation；
  - task_id → Task；
  - actor_id → Actor；
  - llm_call_id → LLMCall，可空。
- 唯一约束：llm_call_id 在非空时唯一，确保一个知识回答调用最多落成一个 AssistantMessage。
- 检查约束：
  - role=USER 时 generation_source=USER 且 llm_call_id 为空；
  - generation_source=LLM 时 role=ASSISTANT 且 llm_call_id 非空；
  - content_text 不为空白。
- 状态字段：无；已保存消息即为不可变事实。
- 时间字段：created_at。
- request_id：普通关联列，不设外键，MVP 默认不单独建索引。
- 错误字段：无；生成失败记录在 LLMCall 和 Task，不创建伪成功消息。
- JSONB：structured_content 可空；普通文本必须保留为普通列。
- 不保存：完整 Prompt、流式 token 片段、模型内部响应对象、图片 bytes 或可变消息草稿。

### 4.4 Task

用途：表示稳定用户目标；NEEDS_INPUT 恢复、整体 Tool 重试和 Explanation 重试均保留同一个 task_id。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| task_id | 是 | opaque id | 主键 |
| conversation_id | 是 | opaque id | 所属 Conversation |
| actor_id | 是 | opaque id | 所有者 |
| task_type | 否 | short text | KNOWLEDGE_QA、TOOL_EXECUTION；意图判定前可空 |
| current_status | 是 | short text | Task 状态 |
| selected_tool_run_id | 否 | opaque id | 当前明确选中的实际 Tool 尝试 |
| selected_result_id | 否 | opaque id | 当前明确选中的 ToolResult |
| created_at | 是 | timestamp | 创建时间 |
| started_at | 否 | timestamp | 首次开始处理时间 |
| updated_at | 是 | timestamp | 状态、错误或 selected 引用最后更新时间 |
| completed_at | 否 | timestamp | 当前终态完成时间 |
| error_code | 否 | short text | 当前 Task 聚合错误码 |
| safe_error_message | 否 | text | 可安全展示或记录的错误摘要 |

Task 状态只允许：

    PENDING
    RUNNING
    NEEDS_INPUT
    SUCCEEDED
    PARTIALLY_SUCCEEDED
    FAILED

- 主键：task_id。
- 外键：
  - conversation_id → Conversation；
  - actor_id → Actor；
  - (task_id, selected_tool_run_id) → ToolRun 的同 Task 候选键；
  - (task_id, selected_tool_run_id, selected_result_id) → ToolResult 的同来源候选键。
- 唯一约束：task_id。除 selected 来源一致性所需约束外，不为重复所有权建立多层复合候选键。
- 选择约束：
  - selected_result_id 非空时 selected_tool_run_id 必须非空；
  - Tool 结果导致的 SUCCEEDED/PARTIALLY_SUCCEEDED 必须同时具有两个 selected 引用；
  - NEEDS_INPUT 与 KNOWLEDGE_QA 不得具有 selected Tool 引用；
  - FAILED 可以只选择失败 ToolRun 而没有 ToolResult。
- 时间语义：
  - created_at 是 Task 建立时间；
  - started_at 是首次开始处理时间；
  - updated_at 随当前聚合事实更新；
  - completed_at 是当前终态时间，NEEDS_INPUT 恢复或显式重试重新进入 RUNNING 时可以清空，下一次终态重新写入；
  - completed_at 不早于 started_at；
  - Task 不保存 duration_ms。需要总墙钟时间时由时间戳计算，该值可能包含用户等待时间，不代表模型或 LLM 执行耗时。
- 错误约束：FAILED 必须有 error_code 或能由选中 ToolRun/LLMCall 给出安全错误；成功状态不保留陈旧 Task 错误。
- JSONB：Task 不设置大 input/result JSONB；输入与结果进入各自实体。
- 不保存：Tool 内部阶段、事件序列、状态转换历史、Tensor、Prompt 或文件 bytes。

### 4.5 TaskInputRevision

用途：保存一个 Task 在某次解析、补充和标准化后的完整输入快照。它支撑 NEEDS_INPUT 恢复和 ToolRun 输入溯源，不是字段级事件流。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| task_input_revision_id | 是 | opaque id | 主键 |
| task_id | 是 | opaque id | 所属 Task |
| request_id | 是 | opaque correlation id | 形成该 Revision 的请求；无 Request 表或外键 |
| source_llm_call_id | 否 | opaque id | 自然语言意图/参数提取 LLMCall；确定性输入可空 |
| revision | 是 | positive integer | Task 内从 1 递增 |
| raw_input | 是 | JSONB | 用户原始数值、单位、材料和 requested outputs 快照 |
| normalized_input | 否 | JSONB | 确定性标准化后的完整输入；未完成时可空或不完整 |
| missing_fields | 是 | text array | 缺失字段名；无缺失时为空数组 |
| ambiguous_fields | 是 | JSONB array | 歧义字段与安全候选摘要 |
| validation_errors | 是 | JSONB array | 结构化校验错误；无错误时为空数组 |
| created_at | 是 | timestamp | 快照创建时间 |

- 主键：task_input_revision_id。
- 外键：
  - task_id → Task.task_id；
  - source_llm_call_id → LLMCall.llm_call_id，可空。
- 唯一约束：(task_id, revision) 唯一。
- 来源规则：
  - 自然语言经 LangChain 提取形成的 Revision 可以引用 purpose=INTENT_AND_PARAMETER_EXTRACTION 的 LLMCall；
  - 直接结构化请求或完全确定性的补充可以为空；
  - Application 校验 source LLMCall 属于同一 Task/request，并区分“LLM 提取错误”和“标准化/校验错误”；
  - LLM structured output 只是候选，TaskInputRevision.raw_input/normalized_input 始终保存 Application 处理后的快照。
- 状态字段：无；缺失、歧义和错误由内容表达，不复制 Task 状态机。
- 时间字段：created_at。
- request_id：普通关联列，不设外键，MVP 默认不单独建索引。
- 错误字段：validation_errors JSONB；平台级执行错误不写入这里。
- JSONB：
  - raw_input、normalized_input 适合 JSONB，因为输入由 schema_version 定义且会随 Tool 演进；
  - ambiguous_fields、validation_errors 适合结构化数组；
  - missing_fields 使用文本数组，便于判断是否为空。
- 不保存：后续 Tool 输出、LLM Prompt、完整 Message 副本、Tensor 或模型运行参数。

### 4.6 TaskInputRevisionMessage

用途：把第二节中的 source_message_ids 变为可校验的关系，记录一个输入快照依据了哪些不可变 Message。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| task_input_revision_id | 是 | opaque id | 输入快照 |
| message_id | 是 | opaque id | 来源 Message |
| source_order | 是 | nonnegative integer | 在该快照中的来源顺序 |

- 主键：(task_input_revision_id, message_id)。
- 外键：
  - task_input_revision_id → TaskInputRevision；
  - message_id → Message。
- 唯一约束：(task_input_revision_id, source_order) 唯一。
- 同一 Task 一致性由创建 Revision 的 Application 短事务校验，不为该冗余所有权建立复合候选键。
- 状态、时间、错误、JSONB：均无。
- 不保存：消息文本副本。

### 4.7 IdempotencyRecord

用途：在 Actor 与操作作用域内绑定幂等 key、请求摘要和实际创建的业务资源，阻止网络重试重复追加 Message、Revision、ToolRun 或 Explanation。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| idempotency_record_id | 是 | opaque id | 主键 |
| actor_id | 是 | opaque id | 幂等主体 |
| operation | 是 | short text | TASK_CREATE、TASK_INPUT_SUPPLEMENT、TOOL_RETRY、EXPLANATION_RETRY |
| idempotency_key | 是 | text | 客户端提供的操作 key |
| request_digest | 是 | fixed digest | 稳定规范请求摘要 |
| first_request_id | 是 | opaque id | 首次接受该 key 的 request_id |
| task_id | 是 | opaque id | 绑定的稳定 Task |
| message_id | 否 | opaque id | TASK_CREATE/SUPPLEMENT 创建的 Message |
| task_input_revision_id | 否 | opaque id | 补充操作创建的 Revision |
| tool_run_id | 否 | opaque id | TOOL_RETRY 创建的 ToolRun |
| explanation_id | 否 | opaque id | EXPLANATION_RETRY 创建的 Explanation |
| created_at | 是 | timestamp | 首次绑定时间 |
| expires_at | 否 | timestamp | 未来保留策略；MVP 可空 |

- 主键：idempotency_record_id。
- 外键：actor_id、task_id 及各可选资源 ID 使用直接普通外键。它们属于同一 Task/Actor 的冗余一致性由创建操作的 Application 短事务校验。
- 唯一约束：(actor_id, operation, idempotency_key) 唯一；first_request_id 唯一。
- 检查约束：operation 决定允许非空的资源引用组合；同 key 同 digest 返回已有 Task，异 digest 冲突。
- 状态字段：无。业务当前状态从绑定的 Task/ToolRun/Explanation 读取，不复制一套幂等状态机。
- 时间字段：created_at、expires_at。
- 错误字段：无；冲突通过 request_digest 比较产生，不覆盖旧记录。
- JSONB：无。request_digest 必须是普通列并可唯一作用域查询。
- 不保存：完整请求正文、完整 Prompt、响应缓存或事件历史。

### 4.8 ToolRun

用途：表示一次实际 MaterialTool.execute 尝试，是 Tool 输入、输出摘要、diagnostics、资产和结果的共同来源锚点。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| tool_run_id | 是 | opaque id | 主键 |
| task_id | 是 | opaque id | 所属稳定 Task |
| request_id | 是 | opaque correlation id | 创建本次执行尝试的请求；无 Request 表或外键 |
| task_input_revision_id | 是 | opaque id | 本次尝试采用的完整输入快照 |
| attempt_no | 是 | positive integer | Task 内 Tool 执行尝试序号 |
| tool_id | 是 | text | 当前为 zta35g_sem_virtual_lab |
| tool_version | 是 | text | 平台集成/校验/执行版本 |
| schema_version | 是 | text | 输入输出结构版本 |
| requested_outputs | 是 | text array | 非空、去重后的请求输出集合 |
| completed_outputs | 是 | text array | 实际完成的请求输出集合 |
| failed_outputs | 是 | text array | 实际失败的请求输出集合 |
| execution_input | 是 | JSONB | 实际发送给 MaterialTool.execute 的已校验载荷 |
| actual_runtime_parameters | 否 | JSONB | seed 等实际运行参数；调用前未知时可空 |
| diagnostics | 是 | JSONB array | 主要步骤轻量诊断；初始为空数组 |
| output_summary | 否 | JSONB | ToolExecutionOutput 的安全摘要 |
| current_status | 是 | short text | ToolRun 状态 |
| model_bundle_id | 否 | text | 内部轻量溯源，不是正式模型版本 |
| created_at | 是 | timestamp | 尝试创建时间 |
| started_at | 否 | timestamp | 实际调用开始时间 |
| completed_at | 否 | timestamp | 终态时间 |
| duration_ms | 否 | nonnegative integer | 尝试耗时 |
| error_code | 否 | short text | 尝试级主错误码 |
| safe_error_message | 否 | text | 安全错误摘要 |

ToolRun 状态只允许：

    PENDING
    RUNNING
    SUCCEEDED
    PARTIALLY_SUCCEEDED
    FAILED

- 主键：tool_run_id。
- 外键：
  - task_id → Task；
  - task_input_revision_id → TaskInputRevision。
- 唯一约束：
  - (task_id, attempt_no) 唯一；
  - (task_id, tool_run_id) 候选键；
  - 不设置 active run 唯一约束。
- 输入链路：

      Message
      → LLM structured output（适用时）
      → TaskInputRevision.raw_input
      → TaskInputRevision.normalized_input
      → ToolRun.execution_input
      → MaterialTool.execute

  task_input_revision_id 指明输入事实来源；execution_input 只冻结本次实际发送载荷；actual_runtime_parameters 保存 Runtime 实际使用的 seed 等参数。ToolRun 不再重复保存 input_snapshot 或 normalized_input。
- 输出集合约束：
  - requested_outputs 非空且元素不重复；
  - completed_outputs 与 failed_outputs 互斥；
  - 二者都是 requested_outputs 的子集；
  - 终态时二者并集等于本次已判定的 requested outputs；
  - 只请求性能且性能失败时 completed_outputs 为空，即使中间 SEM 已保存。
- diagnostics 最小结构包含 step、status、started_at、completed_at、duration_ms、error_code、safe_error_message。
- 状态约束：
  - SUCCEEDED：completed=requested 且 failed 为空；
  - PARTIALLY_SUCCEEDED：completed 和 failed 均非空；
  - FAILED：没有用户请求输出成功，或必需资产/结果提交失败。
- request_id：普通关联列，不设外键，MVP 默认不单独建索引。
- JSONB：execution_input、actual_runtime_parameters、diagnostics、output_summary。
- 不保存：图片 bytes、torch.Tensor、DenseNet 特征、SVR 中间值、模型激活、Remote 临时 token 或内部模型对象。

### 4.9 Asset

用途：保存 MinIO 文件的关系型元数据和跨存储生命周期。对象本体只进入 MinIO。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| asset_id | 是 | opaque id | 主键 |
| task_id | 是 | opaque id | 所属 Task |
| producer_tool_run_id | 否 | opaque id | 生成来源 ToolRun；上传资产为空 |
| actor_id | 是 | opaque id | 所有者 |
| status | 是 | short text | PENDING、AVAILABLE、FAILED、ORPHANED |
| asset_type | 是 | short text | 当前为 sem_image |
| source_type | 是 | short text | GENERATED 或未来 UPLOADED；MVP 只实现 GENERATED |
| role | 是 | short text | requested_output、intermediate 或 supporting |
| object_key | 是 | text | MinIO 内部对象键，不对公共 API 暴露 |
| mime_type | 否 | text | AVAILABLE 时必填 |
| width | 否 | positive integer | 图像宽；AVAILABLE 图像必填 |
| height | 否 | positive integer | 图像高；AVAILABLE 图像必填 |
| bit_depth | 否 | positive integer | 当前正式 PNG 为 8 |
| sha256 | 否 | fixed digest | 最终文件 bytes 的 SHA-256 |
| byte_size | 否 | nonnegative integer | 最终对象字节数 |
| pending_since | 是 | timestamp | stale PENDING 查询锚点 |
| created_at | 是 | timestamp | Asset PENDING 记录创建时间 |
| available_at | 否 | timestamp | 进入 AVAILABLE 的时间 |
| failed_at | 否 | timestamp | 进入 FAILED 的时间 |
| orphaned_at | 否 | timestamp | 进入 ORPHANED 的时间 |
| error_code | 否 | short text | 编码、上传或元数据错误 |
| safe_error_message | 否 | text | 安全错误摘要 |
| orphan_reason | 否 | short text | orphan object/metadata/broken reference 分类 |
| orphan_details | 否 | JSONB | HEAD 核验等安全摘要，不含密钥 |

- 主键：asset_id。
- 外键：
  - task_id → Task；
  - actor_id → Actor；
  - producer_tool_run_id → ToolRun，可空。
- 唯一约束：
  - object_key 全局唯一；
  - (task_id, producer_tool_run_id, asset_id) 候选键，供生成资产的 ResultAssetLink 校验来源。
- 来源约束：
  - source_type=GENERATED 时 producer_tool_run_id 必填；
  - source_type=UPLOADED 时 producer_tool_run_id 必须为空；
  - 当前 MVP 只创建 GENERATED Asset，不实现上传 API、Upload 实体或 ToolRunInputAssetLink；
  - producer ToolRun 必须属于同一 task_id，该一致性由生成资产事务校验。
- 状态约束：
  - PENDING：pending_since 与 object_key 必填，sha256/byte_size 可以为空；
  - AVAILABLE：mime_type、sha256、byte_size、available_at 必填；图像还需 width、height、bit_depth；
  - FAILED：error_code 与 failed_at 必填；
  - ORPHANED：orphan_reason 与 orphaned_at 必填；
  - 只有 AVAILABLE 可进入成功或部分成功 ToolResult 的 artifacts。
- 中间 SEM：只请求 mechanical_properties 时 role=intermediate，但仍以 producer_tool_run_id 关联本次 ToolRun 并执行完整 Asset 生命周期。
- 未来上传：Actor/Task 可以直接拥有 source_type=UPLOADED 的 Asset；未来若 Tool 使用输入资产，可新增轻量输入关联，不修改 Asset 主体结构。本轮不提前创建该关联。
- JSONB：只有 orphan_details 适合 JSONB；常查文件元数据全部为普通列。
- 不保存：PNG bytes、MinIO 密钥、永久公开 URL、宿主机路径、Tensor 或模型特征。

### 4.10 ToolResult

用途：保存一个 ToolRun 规范化后的不可变结构化业务结果。它与自然语言 Explanation 分离。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| result_id | 是 | opaque id | 主键 |
| task_id | 是 | opaque id | 所属 Task |
| tool_run_id | 是 | opaque id | 唯一实际来源 ToolRun |
| actor_id | 是 | opaque id | 所有者，用于未来访问控制 |
| status | 是 | short text | SUCCEEDED、PARTIALLY_SUCCEEDED 或 FAILED |
| requested_outputs | 是 | text array | 请求输出集合快照 |
| completed_outputs | 是 | text array | 成功的用户请求输出 |
| failed_outputs | 是 | text array | 失败的用户请求输出 |
| data | 是 | JSONB object | Tool 特有结构化数据；无数据时为空对象 |
| warnings | 是 | JSONB array | 结构化警告；无警告时为空数组 |
| provenance | 是 | JSONB object | 输入 revision、运行参数、资产 ID、bundle 轻量身份等 |
| error | 否 | JSONB object | code、safe_message 和可选安全细节 |
| tool_id | 是 | text | Tool 身份快照 |
| tool_version | 是 | text | 正式 Tool 版本 |
| schema_version | 是 | text | 正式 Schema 版本 |
| created_at | 是 | timestamp | 结果提交时间 |

逻辑公共字段 artifacts 不作为自由 JSONB 存储，而由 ResultAssetLink + Asset 组装，保持第一、第二节公共 ToolResult 契约不变。

- 主键：result_id。
- 外键：
  - task_id → Task；
  - tool_run_id → ToolRun；
  - actor_id → Actor。
- 唯一约束：
  - tool_run_id 唯一，即一个 ToolRun 最多一个 ToolResult；
  - (task_id, tool_run_id, result_id) 候选键，供 Task selected 复合外键和 ResultAssetLink 使用。
- 输出集合约束与 ToolRun 相同；ToolResult 的集合必须与来源 ToolRun 终态快照一致。
- 版本一致性：tool_id、tool_version、schema_version 必须与来源 ToolRun 一致，由结果提交 Application 短事务校验；不为这些重复快照增加额外复合外键。
- 状态约束：
  - SUCCEEDED：全部 requested outputs 完成；
  - PARTIALLY_SUCCEEDED：至少一个完成且至少一个失败；
  - FAILED：没有用户请求输出成功；可以保存结构化失败结果，但不得包含不可追溯的性能成功数据。
- 时间字段：created_at；结果不可变，不设置 updated_at。
- 错误字段：error JSONB。高频排错仍使用 Task/ToolRun 的普通 error_code。
- JSONB：data、warnings、provenance、error。
- 不保存：完整文件 bytes、object_key、Tensor、DenseNet 特征、SVR 中间值、模型激活、完整 Prompt、编造的 confidence/OOD/applicability。

### 4.11 ResultAssetLink

用途：把 ToolResult.artifacts 映射为可受外键约束的 Asset 集合，并阻止跨 ToolRun 混合。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| task_id | 是 | opaque id | 来源 Task |
| tool_run_id | 是 | opaque id | 来源 ToolRun |
| result_id | 是 | opaque id | ToolResult |
| asset_id | 是 | opaque id | Asset |
| artifact_order | 是 | nonnegative integer | API 展示的稳定顺序 |
| created_at | 是 | timestamp | 结果提交时创建 |

- 主键：(result_id, asset_id)。
- 外键：
  - (task_id, tool_run_id, result_id) → ToolResult；
  - (task_id, tool_run_id, asset_id) → Asset(task_id, producer_tool_run_id, asset_id)。
- 唯一约束：
  - asset_id 唯一，MVP 中一个 Asset 最多进入一个 ToolResult；
  - (result_id, artifact_order) 唯一。
- 检查约束：
  - 两个高价值复合外键直接保证 Result 与生成 Asset 的 task_id/tool_run_id 来源一致；
  - 关联时 Asset.status 必须为 AVAILABLE；这是跨行事务不变量，由结果提交服务在同一短事务中锁定并复核；
  - UPLOADED Asset 没有 producer_tool_run_id，不能直接进入当前生成结果的 ResultAssetLink。未来输入资产关系由届时新增的轻量关联表达。
- 状态、错误、JSONB：均无。
- 不保存：Asset 元数据副本或 object_key。

### 4.12 NaturalLanguageExplanation

用途：保存对同一 ToolResult 的一次自然语言解释尝试。重试追加新行，不覆盖旧尝试。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| explanation_id | 是 | opaque id | 主键 |
| task_id | 是 | opaque id | 所属 Task |
| result_id | 是 | opaque id | 固定输入 ToolResult |
| llm_call_id | 是 | opaque id | 本次生成调用 |
| attempt_no | 是 | positive integer | Result 内解释尝试序号 |
| status | 是 | short text | PENDING、RUNNING、SUCCEEDED、FAILED |
| language | 是 | short text | 解释语言 |
| text | 否 | text | 成功时的正式解释 |
| created_at | 是 | timestamp | 尝试创建时间 |
| started_at | 否 | timestamp | LLM 调用开始时间 |
| completed_at | 否 | timestamp | 终态时间 |
| duration_ms | 否 | nonnegative integer | 耗时 |
| error_code | 否 | short text | 解释级错误码 |
| safe_error_message | 否 | text | 安全错误摘要 |

- 主键：explanation_id。
- 外键：
  - task_id → Task；
  - result_id → ToolResult；
  - llm_call_id → LLMCall。
- 唯一约束：
  - (result_id, attempt_no) 唯一；
  - llm_call_id 唯一，一次 LLMCall 最多对应一个 Explanation。
- result_id、task_id、llm_call_id 的同一来源关系由 Explanation 准备事务校验，不强制多层复合候选键。
- 状态约束：
  - SUCCEEDED 时 text 非空且错误为空；
  - FAILED 时 text 为空，error_code 非空；
  - Explanation 只能读取已持久化 ToolResult。
- 时间字段：created_at、started_at、completed_at、duration_ms。
- JSONB：MVP 不需要；生成参数在 LLMCall。
- 不保存：ToolResult 副本、Prompt、流式 token、模型原始响应对象。

### 4.13 LLMCall

用途：记录意图/参数提取、知识回答或 ToolResult Explanation 的一次外部 LLM 调用尝试，满足输入变化、结构化提取、耗时、模型标识、参数和安全错误排查。

| 字段 | 必填 | 逻辑类型 | 说明 |
|---|---|---|---|
| llm_call_id | 是 | opaque id | 主键 |
| task_id | 是 | opaque id | 所属 Task |
| conversation_id | 是 | opaque id | 所属 Conversation |
| request_id | 是 | opaque correlation id | 发起本次调用的请求；无 Request 表或外键 |
| purpose | 是 | short text | INTENT_AND_PARAMETER_EXTRACTION、KNOWLEDGE_ANSWER 或 TOOL_RESULT_EXPLANATION |
| input_result_id | 否 | opaque id | Explanation 调用的输入 ToolResult |
| provider | 是 | text | LLM 提供方标识 |
| model_name | 是 | text | 实际调用模型标识 |
| prompt_template_id | 否 | text | 受控模板身份 |
| prompt_template_version | 否 | text | 模板版本 |
| prompt_digest | 否 | fixed digest | 对受控规范 Prompt 的摘要，不是 Prompt 内容 |
| generation_parameters | 是 | JSONB object | temperature、max tokens 等实际参数 |
| structured_output_summary | 否 | JSONB object | 安全结构化摘要；意图提取调用使用 |
| usage | 否 | JSONB object | provider 返回的 token/usage 安全摘要 |
| provider_request_id | 否 | text | 外部提供方请求标识，用于排错 |
| status | 是 | short text | PENDING、RUNNING、SUCCEEDED、FAILED |
| created_at | 是 | timestamp | 调用记录创建时间 |
| started_at | 否 | timestamp | 网络调用开始时间 |
| completed_at | 否 | timestamp | 终态时间 |
| duration_ms | 否 | nonnegative integer | 网络调用耗时 |
| error_code | 否 | short text | 规范化 LLM 错误码 |
| safe_error_message | 否 | text | 安全错误摘要 |

- 主键：llm_call_id。
- 外键：
  - task_id → Task；
  - conversation_id → Conversation；
  - input_result_id → ToolResult，可空。
- 唯一约束：
  - (provider, provider_request_id) 在 provider_request_id 非空时可设唯一；
  - 不以模型响应文本做唯一判断。
- purpose 约束：
  - INTENT_AND_PARAMETER_EXTRACTION 的 input_result_id 必须为空；structured_output_summary 可以保存知识/Tool 意图、候选 tool_id、参数值与单位候选、requested_outputs、missing_fields 和 ambiguous_fields 的安全摘要；
  - TOOL_RESULT_EXPLANATION 必须有 input_result_id，并由 NaturalLanguageExplanation 引用；
  - KNOWLEDGE_ANSWER 的 input_result_id 必须为空，成功时由一个 AssistantMessage 引用；
  - 失败意图调用可以没有 TaskInputRevision，失败知识调用可以没有 AssistantMessage。
- 排错边界：
  - TaskInputRevision.source_llm_call_id 允许把“LLM 提取结果”与后续 Application 标准化/校验分开定位；
  - structured_output_summary 不是最终输入事实，不得绕过确定性标准化、范围和精度校验；
  - task_id、conversation_id、input_result_id 与输出实体的冗余一致性由对应 Application 短事务校验。
- 状态与时间约束与 Explanation 相同。
- JSONB：generation_parameters、structured_output_summary、usage；provider/model/purpose 等常查字段保持普通列。
- 不保存：完整 Prompt、密钥、敏感用户数据、流式 token、完整 provider 响应、Tool Tensor 或图片 bytes。

## 5. 关系、基数与来源一致性

### 5.1 核心基数

| 关系 | 基数 | 规则 |
|---|---|---|
| Actor → Conversation | 1:N | 每个 Conversation 只有一个所有者 |
| Conversation → Message | 1:N | 消息按 created_at + message_id 稳定排序 |
| Conversation → Task | 1:N | 每个 Task 只属于一个 Conversation |
| Task → Message | 1:N | 初始消息、补充消息和回答均绑定稳定 Task |
| Task → TaskInputRevision | 1:N | Tool 路径至少一个；知识问答可以为零 |
| LLMCall → TaskInputRevision | 1:0..N | 每个 Revision 最多引用一个意图提取调用；确定性输入可以不引用 |
| Task → ToolRun | 1:N | 每次实际执行或整体重试追加一个 |
| Actor/Task → Asset | 1:N | 每个 Asset 必须有所有者和所属 Task |
| ToolRun → Asset | 1:0..N | GENERATED Asset 有一个 producer；UPLOADED Asset 没有 producer |
| ToolRun → ToolResult | 1:0..1 | 结果组装前失败可以没有 Result |
| ToolResult → Asset | 1:N，经 ResultAssetLink | 当前 artifacts 只引用同 producer ToolRun 的 AVAILABLE GENERATED Asset |
| ToolResult → Explanation | 1:N | 每次解释重试追加一个尝试 |
| Explanation → LLMCall | 1:1 | 每个解释尝试只对应一次调用 |
| LLMCall → AssistantMessage | 1:0..1 | 仅知识回答成功时产生 |

### 5.2 selected 引用

Task 的选择不是“最新创建”或“最后完成”的隐式规则：

1. selected_tool_run_id 必须指向当前 task_id 下的 ToolRun。
2. selected_result_id 非空时，必须指向 selected_tool_run_id 产生的 ToolResult。
3. selected_result_id 不得单独存在。
4. 结果提交事务同时更新两个引用，避免瞬时混搭。
5. ToolRun 失败且没有 ToolResult 时，可以只选择该 ToolRun，用其错误解释 Task 失败。
6. 知识问答和 NEEDS_INPUT 的两个引用均为空。

这两组属于需要数据库优先直接保证的少量高价值复合外键：

    Task(task_id, selected_tool_run_id)
      → ToolRun(task_id, tool_run_id)

    Task(task_id, selected_tool_run_id, selected_result_id)
      → ToolResult(task_id, tool_run_id, result_id)

### 5.3 Asset 与 ToolResult 不混合

ResultAssetLink 同时携带 task_id 和 tool_run_id，并分别对 ToolResult 与 Asset(task_id, producer_tool_run_id, asset_id) 使用复合外键。因此数据库可阻止：

- ToolRun A 的 Result 引用 ToolRun B 的 Asset；
- Task A 的 Result 引用 Task B 的 Asset；
- 一个已属于旧 Result 的 Asset 被新 Result 重用；
- 没有 producer ToolRun 的 UPLOADED Asset 被当作当前生成结果 artifact。

Asset.status=AVAILABLE 需要在结果提交事务中锁定并复核；普通静态外键不能表达跨行状态条件。

### 5.4 意图提取与执行输入链

自然语言 Tool 请求的数据链路为：

    Message(request_id)
    → LLMCall(INTENT_AND_PARAMETER_EXTRACTION, request_id)
    → structured_output_summary（候选）
    → TaskInputRevision.raw_input
    → TaskInputRevision.normalized_input
    → ToolRun.execution_input
    → MaterialTool.execute

- LLMCall 摘要可以表达候选 tool_id、值/单位、requested outputs、missing/ambiguous fields；
- TaskInputRevision.source_llm_call_id 用于追溯提取来源；
- Application 必须把 LLM 候选转成 raw_input，并执行确定性标准化和完整校验后才能形成 normalized_input；
- ToolRun.execution_input 是唯一 attempt 级执行载荷，不能反向覆盖 Revision；
- 直接结构化请求或完全确定性的补充可以不创建意图提取 LLMCall，source_llm_call_id 为空。

### 5.5 只请求性能的中间 SEM

只请求 mechanical_properties 时：

- ToolRun.requested_outputs 只有 mechanical_properties；
- Tool 内部生成的 SEM 建立 Asset，source_type=GENERATED、producer_tool_run_id=当前 ToolRun、role=intermediate；
- Asset.completed 与用户输出集合无关；
- 性能成功时，该中间 Asset 仍通过 ResultAssetLink 进入 provenance/artifacts 的受控来源集合；
- 性能失败时 Asset 可保留 AVAILABLE，但 ToolRun.completed_outputs 为空，Task 不得部分成功。

### 5.6 NEEDS_INPUT 与幂等

- NEEDS_INPUT 恢复保留 Task，追加带新 request_id 的 User Message、TaskInputRevision 和来源关联。
- 自然语言补充可以创建新的 INTENT_AND_PARAMETER_EXTRACTION LLMCall 并由新 Revision 引用；确定性补充可以不调用 LLM。
- 不覆盖旧 Message 或 Revision。
- 同一补充 operation/key/digest 命中 IdempotencyRecord 时，直接返回原 Task 当前事实。
- 唯一约束冲突必须先读取既有 request_digest；相同则复用，差异则冲突。
- 重复提交不得先写 Message/Revision/ToolRun 再检查幂等。

## 6. 普通列与 JSONB 边界

### 6.1 字段分配

| 内容 | 推荐表示 | 原因 |
|---|---|---|
| 所有主键、外键、actor_id | 普通列 | 关联、唯一性和访问控制 |
| Message/TaskInputRevision/ToolRun/LLMCall.request_id | 普通 opaque correlation 列 | 串联数据库事实与结构化日志；无 Request 表 |
| current_status、role、type、purpose | 普通短文本列 + CHECK | 高频筛选和稳定约束 |
| tool_id、tool_version、schema_version、model_bundle_id | 普通列 | 来源查询与一致性校验 |
| Task.created/started/updated/completed 时间 | 普通列 | Task 墙钟范围；不保存 Task.duration_ms |
| ToolRun/LLMCall/Explanation.duration_ms | 普通列 | 具体执行耗时 |
| error_code、safe_error_message（Task/ToolRun/Asset/Explanation/LLMCall） | 普通列 | 高频排错 |
| TaskInputRevision.raw_input | JSONB object | Tool 特有原始结构 |
| TaskInputRevision.normalized_input | JSONB object | Tool 特有标准化结构 |
| missing_fields | text array | 小集合、常判断是否为空 |
| ambiguous_fields、validation_errors | JSONB array | 结构化详情会演进 |
| ToolRun.requested/completed/failed outputs | text array | 需要集合约束，不使用任意 JSON |
| ToolRun.execution_input | JSONB object | 实际发送给 MaterialTool.execute 的唯一 attempt 级载荷 |
| ToolRun.actual_runtime_parameters | JSONB object | Tool/Runtime 特有参数 |
| ToolRun.diagnostics | JSONB array | 轻量步骤列表，不拆 StageRun |
| ToolRun.output_summary | JSONB object | 安全摘要随 Tool 演进 |
| ToolResult.data | JSONB object | Tool 特有结构化结果 |
| ToolResult.warnings | JSONB array | 警告集合会扩展 |
| ToolResult.provenance | JSONB object | 低频、版本化来源详情 |
| ToolResult.error | JSONB object/空 | Result 级可变错误详情 |
| ToolResult.artifacts | 关系投影 | ResultAssetLink + Asset，保留外键完整性 |
| LLM generation_parameters | JSONB object | provider 参数存在差异 |
| LLM structured_output_summary | JSONB object/空 | 意图、候选 Tool/参数/单位/outputs/missing/ambiguous 的安全摘要 |
| LLM usage | JSONB object | MVP 不计费，provider 字段存在差异 |
| Message.content_text | 普通 text | 展示、检索和不可变事实 |

### 6.2 JSONB 规则

1. 每个 JSONB 必须由对应 schema_version 或应用内部结构校验，不接受任意无界对象。
2. 重复的公共标识不以 JSONB 为唯一事实；task_id、tool_run_id、tool/version/schema 均保留普通列。
3. provenance 中可以重复少量人类可读快照，但完整性约束以普通列和外键为准。
4. 不在 MVP 为 JSONB 建通用 GIN 索引；只有出现稳定查询路径后再增加表达式或局部索引。
5. diagnostics 保持主要步骤摘要，不保存完整 Trace/Span、Tensor 统计或模型激活。
6. JSONB 中禁止 object_key、密钥、完整 Prompt、图片 bytes 和敏感用户数据。
7. LLM structured_output_summary 只保留安全候选摘要；TaskInputRevision 才是 Application 处理后的输入快照，ToolRun.execution_input 才是实际执行载荷。

### 6.3 outputs 为什么不用 JSONB

requested/completed/failed outputs 是每个 ToolRun/ToolResult 都存在的公共集合，需要检查：

- requested 非空；
- completed 与 failed 互斥；
- completed/failed 是 requested 子集；
- 状态与集合一致。

因此已确认使用 PostgreSQL text array。它比三张输出明细表简单，又比 JSONB 更容易做集合检查。当前不为数组建立 GIN 索引，因为 MVP 没有“按某个 output 搜索所有历史结果”的实际查询。

## 7. 唯一约束、外键与检查约束

### 7.1 数据库优先直接保证的高价值不变量

| 不变量 | 逻辑约束 |
|---|---|
| 幂等作用域唯一 | IdempotencyRecord(actor_id, operation, idempotency_key) UNIQUE |
| TaskInputRevision revision 唯一 | (task_id, revision) UNIQUE |
| ToolRun attempt_no 唯一 | (task_id, attempt_no) UNIQUE |
| Asset object_key 唯一 | object_key UNIQUE |
| 每个 ToolRun 最多一个 ToolResult | ToolResult.tool_run_id UNIQUE |
| ResultAssetLink 不混合不同 Task/ToolRun 的生成 Asset | Link 对 ToolResult(task_id, tool_run_id, result_id) 和 Asset(task_id, producer_tool_run_id, asset_id) 使用复合外键 |
| Task selected ToolRun/Result 来源一致 | Task 对 ToolRun(task_id, tool_run_id) 和 ToolResult(task_id, tool_run_id, result_id) 使用复合外键 |

数据库还应直接保证主键、必要普通外键、ResultAssetLink 主键/asset_id 唯一、(result_id, attempt_no)、非空 LLM Message/Explanation 的 llm_call_id 唯一，以及简单行内 CHECK。这些约束具有明确业务价值，不是为了重复所有权而堆叠候选键。

### 7.2 Application 短事务校验的冗余一致性

以下规则需要成立，但不要求阶段 1A 全部实现成物理复合外键：

- Message.actor_id 与其 Task/Conversation.actor_id 一致；
- Message、TaskInputRevision、ToolRun、LLMCall 的 request_id 与当前操作关联正确；
- TaskInputRevision.source_llm_call_id 属于同一 Task/request，且 purpose 为 INTENT_AND_PARAMETER_EXTRACTION；
- TaskInputRevisionMessage 中的 Message 与 Revision 属于同一 Task；
- LLMCall.conversation_id 与 Task.conversation_id 一致；
- KNOWLEDGE_ANSWER、TOOL_RESULT_EXPLANATION 和 INTENT_AND_PARAMETER_EXTRACTION 的输入/输出实体匹配 purpose；
- ToolRun.task_input_revision_id 属于同一 Task，execution_input 由该 Revision 的 normalized_input 生成；
- GENERATED Asset 的 producer ToolRun 属于同一 Task；UPLOADED Asset 没有 producer；
- ToolResult.actor_id、tool_id、tool_version、schema_version 与 Task/ToolRun 重复快照一致；
- Explanation.task_id、result_id、llm_call_id 属于同一解释尝试；
- IdempotencyRecord 的可选 message/revision/tool_run/explanation 引用属于同一 Task/Actor。

这些检查与对应写入在同一短 PostgreSQL 事务中完成。它们仍需契约测试，但不要求为每个重复 actor/task/conversation 列建立多层复合候选键。

### 7.3 行内 CHECK 与跨行状态校验

适合数据库行内 CHECK：

- ToolRun/ToolResult 的 requested/completed/failed 集合包含关系和状态组合；
- source_type=GENERATED 时 producer_tool_run_id 非空，source_type=UPLOADED 时为空；
- duration_ms、byte_size、width、height、bit_depth、revision、attempt_no 的非负/正数约束；
- completed_at 不早于 started_at；
- Task/ToolRun/Asset/ToolResult/Explanation/LLMCall 状态枚举及状态依赖字段；
- Task selected_result_id 非空时 selected_tool_run_id 也非空。

需要 Application 在短事务中锁定并复核：

- Asset.status=AVAILABLE 才能建立 ResultAssetLink；
- Task 更新 selected 引用时目标 ToolRun/Result 已达到允许状态；
- ToolResult 输出集合和 ToolRun 终态快照一致；
- Explanation 只读取已提交 ToolResult；
- 并发分配 revision、attempt_no 和 explanation attempt_no 时正确处理唯一冲突。

阶段 1A 不要求使用数据库触发器，也不要求把所有冗余一致性升级为复合外键。优先采用直接外键、上述少量高价值复合约束、短事务与 Repository 条件更新。

## 8. 索引建议

### 8.1 MVP 必要索引

| 查询 | 建议索引 | 说明 |
|---|---|---|
| Actor 的 Conversation 列表 | Conversation(actor_id, updated_at DESC, conversation_id) | 支持会话列表 |
| Conversation 消息排序 | Message(conversation_id, created_at, message_id) | 稳定时间排序 |
| Task 按所有者/状态/时间 | Task(actor_id, current_status, created_at DESC, task_id) | 用户任务列表和状态筛选 |
| Conversation 下 Task | Task(conversation_id, created_at, task_id) | 会话详情 |
| Task 下 ToolRun | ToolRun(task_id, attempt_no) UNIQUE | 同时满足查询与并发序号约束 |
| ToolRun 生成的 Asset | Asset(producer_tool_run_id, created_at, asset_id)，producer 非空局部索引 | 结果组装与排错 |
| stale Asset | Asset(status, pending_since)；可做 PENDING/ORPHANED 局部索引 | 恢复与 orphan 扫描 |
| object_key 查找 | Asset(object_key) UNIQUE | Storage 回查与完整性 |
| ToolRun 的 Result | ToolResult(tool_run_id) UNIQUE | 一次运行最多一个结果 |
| Result/语言的最近成功 Explanation | NaturalLanguageExplanation(result_id, language, completed_at DESC, explanation_id DESC)，status=SUCCEEDED 局部索引 | 已确认确定性读取规则 |
| Task 的 LLM 调用 | LLMCall(task_id, created_at, llm_call_id) | 知识问答/解释排错 |
| 幂等命中 | IdempotencyRecord(actor_id, operation, idempotency_key) UNIQUE | 并发去重 |

外键列若未被上述索引覆盖，应补最小 B-tree 索引；不为每个状态或低基数字段单独建索引。

Message.request_id、TaskInputRevision.request_id、ToolRun.request_id 和 LLMCall.request_id 默认不单独建索引。它们首先用于把数据库事实与结构化日志串联；只有出现真实的数据库内 request_id 查询需求时再增加索引。

### 8.2 Asset SHA-256

MVP 不默认为 sha256 建索引，原因：

- SHA-256 当前用于完整性检查和 orphan 恢复，不做跨用户内容去重；
- 正常恢复先通过 asset_id/object_key 定位，不依赖全表 hash 搜索；
- 额外索引会增加每次 AVAILABLE 更新成本。

只有阶段 1A/运维验证出现以下真实查询时才增加 sha256 局部索引：

- 按 hash 查找重复对象；
- 仅凭 MinIO metadata 中的 hash 反查 Asset；
- 定期执行大规模 checksum 核验。

即使增加 hash 索引，也不得仅以 sha256 认定两个 Asset 可共享所有权或来源。

### 8.3 暂不建立的索引

- 不为 ToolRun.diagnostics、ToolResult.data/warnings/provenance 建通用 GIN；
- 不为 requested_outputs 建 GIN；
- 不为 status 单列建低选择性索引；
- 不为 model_bundle_id 建索引，除非阶段 1B 出现实际事故检索需求；
- 不为 Event、Trace、Stage 建索引，因为不存在这些业务表。

## 9. 事务边界映射

### 9.1 总原则

每个数据库事务只覆盖需要共同成功的 PostgreSQL 事实。下列外部操作必须发生在事务之外：

- MaterialTool/Runtime 调用和模型计算；
- PNG 编码；
- MinIO put/head/delete；
- LLM 网络调用；
- TaskProgressReporter 调用和结构化日志写出。

原因是这些操作耗时不确定、不能由 PostgreSQL 回滚。把它们放进长事务会长期占用连接和行锁，扩大并发冲突，并在外部成功、数据库回滚时制造更难恢复的不一致。

### 9.2 事务 1：请求创建

同一短事务共同提交：

- 确认或创建 Actor；
- 确认或创建 Conversation；
- 创建 Task PENDING；
- 追加带当前 request_id 的初始 User Message；
- 创建 IdempotencyRecord 并绑定 actor/operation/key/digest/task/message。

这些实体必须共同成功，否则会出现无消息 Task、无 Task 消息或不能重放的幂等占位。意图解析 LLM 网络调用不进入该事务。

### 9.3 意图与参数提取 LLMCall

自然语言请求在 Task/User Message 已提交后使用“准备短事务 → 网络调用 → 终结短事务”：

1. 准备事务创建 LLMCall PENDING，purpose=INTENT_AND_PARAMETER_EXTRACTION，并保存 task_id、conversation_id、request_id、模板/模型/参数安全元数据。
2. 在数据库事务外调用 LLM，提取知识/Tool 意图、候选 tool_id、参数值/单位、requested outputs 和 missing/ambiguous fields。
3. 终结事务保存 LLMCall SUCCEEDED/FAILED、耗时、错误及 structured_output_summary；不保存完整 Prompt 或 provider 响应。
4. Application 在事务外把成功候选执行确定性标准化和校验，再进入 Revision 事务。

直接结构化请求或完全确定性的补充可以跳过该 LLMCall。失败意图调用不创建伪造 Revision；Task 保存可定位错误或进入明确的恢复路径。

### 9.4 事务 2：NEEDS_INPUT revision 追加

补充请求的同一短事务共同提交：

- 新 IdempotencyRecord；
- 新带 request_id 的 User Message；
- 新 TaskInputRevision，保存 request_id 和可空 source_llm_call_id；
- TaskInputRevisionMessage 来源关联；
- Task.current_status 更新为 NEEDS_INPUT、RUNNING 或 FAILED；
- 必要的安全校验错误。

同 key 同 digest 命中既有记录时，这些实体一个也不追加。全量标准化在事务前完成，事务只保存 Application 已处理快照；不锁表等待 LLM。source_llm_call_id 只用于来源排错，不能让 LLM 候选绕过最终校验。

初次解析即发现缺失/歧义时，也使用相同 Revision 事务保存 revision 1。初次输入完整合法时，可在事务 3 中把 revision 1 与 ToolRun 一起提交。

### 9.5 事务 3：ToolRun 创建/启动

实际执行前的短事务共同提交：

- 若尚无最终合法 Revision，则创建该 TaskInputRevision 及来源关联；
- 创建新 ToolRun，保存当前 request_id、分配 attempt_no/tool/version/schema，并写入从该 Revision 派生的 execution_input；
- 显式重试时同时创建/绑定 IdempotencyRecord；
- Task 进入 RUNNING；
- ToolRun 从 PENDING 进入 RUNNING并写 started_at。

提交后才把 execution_input 传给 MaterialTool.execute。若提交失败，不能调用 Tool；若 Runtime 调用失败，后续用终结事务保存 ToolRun/Task 失败，不回滚已存在的执行尝试。

### 9.6 事务 4：Asset PENDING

Tool 返回 ToolExecutionOutput 后，同一短事务：

- 将安全 diagnostics、actual_runtime_parameters、output_summary 写入 ToolRun；
- 为每个必须保存的图片创建 Asset PENDING；
- 生成并保存唯一 object_key、pending_since、source_type=GENERATED 和 producer_tool_run_id。

事务提交后才编码 PNG、计算 SHA-256 并上传 MinIO。若 Tool 未返回图片且运行已经失败，可跳过 Asset，直接进入事务 6 的失败终结。

### 9.7 事务 5：Asset AVAILABLE/FAILED/ORPHANED

每个 Asset 使用独立短事务做条件更新：

- 上传成功且元数据核验通过：PENDING → AVAILABLE，并保存 mime/尺寸/位深/hash/byte_size；
- 编码或上传确定失败：PENDING → FAILED，并保存错误；
- 对象与数据库状态不一致：PENDING/AVAILABLE → ORPHANED，并保存 orphan 信息；
- 恢复核验一致：ORPHANED/PENDING → AVAILABLE。

MinIO put/head/delete 发生在这些事务之间，不跨事务持锁。状态更新使用当前状态条件，防止并发恢复器覆盖已终结事实。

### 9.8 事务 6：ToolResult + ToolRun 终态 + Task selected

结果提交短事务共同完成：

- 锁定并复核本次 ToolRun；
- 复核所有拟引用 Asset 属于同一 task_id、producer_tool_run_id 等于本次 ToolRun 且为 AVAILABLE；
- 创建不可变 ToolResult；
- 创建 ResultAssetLink；
- 将 ToolRun 更新到 SUCCEEDED/PARTIALLY_SUCCEEDED/FAILED；
- 更新 Task.current_status、selected_tool_run_id、selected_result_id 和错误；
- 复核输出集合、版本、ResultAssetLink 来源复合外键和 selected 来源复合外键。

这组事实不能拆开，否则可能出现 Task 指向不存在的 Result、Result 引用非 AVAILABLE Asset，或 ToolRun 已成功但 Result 未提交。

若 Runtime 在结果组装前失败且没有 ToolResult，则同类短事务只终结 ToolRun/Task，并允许 Task 只选择失败 ToolRun。

### 9.9 事务 7：Explanation/LLMCall + Task 最终状态

为避免 LLM 网络调用进入长事务，该逻辑阶段分为两个短事务：

1. 准备事务：创建 LLMCall PENDING 和 NaturalLanguageExplanation PENDING，二者引用同一 Task/ToolResult；解释重试的 IdempotencyRecord 同时绑定 explanation_id。
2. 网络事务外：读取已持久化 ToolResult，调用 LLM。
3. 终结事务：
   - 成功时共同提交 LLMCall SUCCEEDED、Explanation SUCCEEDED/text 和 Task SUCCEEDED；
   - 失败时共同提交 LLMCall FAILED、Explanation FAILED/error 和 Task PARTIALLY_SUCCEEDED。

ToolResult 与 ToolRun 不因解释失败而改写。重试继续引用同一 result_id。

### 9.10 事务 8：知识问答 AssistantMessage/LLMCall + Task

意图提取 LLMCall 已判定为知识问答后，回答调用使用“准备短事务 → 网络调用 → 终结短事务”：

1. 创建 LLMCall PENDING，purpose=KNOWLEDGE_ANSWER，并保存当前 request_id。
2. 在事务外调用 LLM。
3. 成功终结事务共同创建带同 request_id 的 AssistantMessage、置 LLMCall SUCCEEDED、置 Task SUCCEEDED。
4. 失败终结事务置 LLMCall/Task FAILED，不创建 AssistantMessage。

整个路径不创建 ToolRun、Asset、ToolResult 或 NaturalLanguageExplanation。

## 10. 主要场景的数据落点

### 10.1 普通知识问答

    Actor
    → Conversation
    → User Message(request_id)
    → Task
    → LLMCall(INTENT_AND_PARAMETER_EXTRACTION, request_id)
    → LLMCall(KNOWLEDGE_ANSWER)
    → Assistant Message(request_id)

Task selected 引用为空。意图提取摘要只保存安全结构化候选；任一 LLMCall 失败时都不伪造后续事实。

### 10.2 NEEDS_INPUT 与恢复

初次不完整：

    User Message(request_id)
    → LLMCall(INTENT_AND_PARAMETER_EXTRACTION，可选)
    → TaskInputRevision revision 1(request_id, source_llm_call_id)
    → Task NEEDS_INPUT
    → 无 ToolRun

补充后仍不完整：

    新 User Message(request_id)
    → 新意图提取 LLMCall（适用时）
    → TaskInputRevision revision 2(request_id, source_llm_call_id)
    → Task 仍 NEEDS_INPUT

补充后完整合法：

    新 User Message(request_id)
    → TaskInputRevision revision 3(request_id, source_llm_call_id 可空)
    → Task RUNNING
    → 新 ToolRun

所有旧 Message/Revision 保留。补充请求的幂等重试不得再追加 revision 3。

### 10.3 Tool 完整成功

    TaskInputRevision
    → ToolRun(request_id, execution_input) RUNNING
    → Asset(source_type=GENERATED, producer_tool_run_id) PENDING
    → Asset AVAILABLE
    → ToolResult SUCCEEDED + ResultAssetLink
    → ToolRun SUCCEEDED
    → Task selected references
    → Explanation + LLMCall
    → Task SUCCEEDED

### 10.4 同时请求两项，性能失败

- ToolRun.requested_outputs = sem_image + mechanical_properties；
- SEM Asset AVAILABLE，source_type=GENERATED、producer_tool_run_id=本次 ToolRun、role=requested_output；
- ToolResult.status = PARTIALLY_SUCCEEDED；
- completed_outputs 只有 sem_image；
- failed_outputs 只有 mechanical_properties；
- ResultAssetLink 只引用本次 ToolRun 的 SEM；
- ToolRun/Task 为 PARTIALLY_SUCCEEDED；
- Explanation 可以解释已有图片和性能失败，不得编造性能值。

### 10.5 只请求性能，性能失败

- 中间 SEM Asset 可为 AVAILABLE，source_type=GENERATED、producer_tool_run_id=本次 ToolRun、role=intermediate；
- ToolRun.completed_outputs 为空；
- ToolResult 若创建则为 FAILED，data 不含性能成功值；
- ToolRun/Task 为 FAILED；
- 中间 Asset 不使 Task 部分成功；
- Task selected_result_id 可指向该失败 Result；若 Runtime 未形成可规范化输出，也可为空。

### 10.6 Asset 保存失败

- Asset 为 FAILED 或可识别的 PENDING/ORPHANED；
- ToolRun 保存 Tool diagnostics/output_summary；
- ToolResult 不得保存不可追溯性能成功数据；
- ToolRun/Task FAILED；
- 不建立指向非 AVAILABLE Asset 的 ResultAssetLink。

### 10.7 整体 Tool 重试

    保留 task_id
    + 新 request_id
    + 新 IdempotencyRecord
    + 新 ToolRun.request_id / tool_run_id / attempt_no
    + 新 Asset / ToolResult
    + 旧运行及产物不可变

新结果提交后，在事务 6 中更新 Task selected 引用。任何查询都不得按 latest 隐式拼装结果。

### 10.8 Explanation 重试

    同一 result_id
    + 新 request_id
    + 新 explanation_id / attempt_no
    + 新 llm_call_id
    + 不创建 ToolRun
    + 不修改 ToolResult

所有失败和成功尝试保留。MVP 已确认不增加 selected_explanation_id；展示层对同一 result_id/language 过滤 SUCCEEDED，并按 completed_at DESC、explanation_id DESC 确定性读取最近一次成功解释。ToolResult 始终是事实来源。

## 11. 登录、Tool 扩展与 SSE 兼容边界

### 11.1 未来登录系统

- MVP 创建稳定 Actor，user_id 为空；Conversation、Task、Asset、ToolResult 保存 actor_id。
- 用户登录后，由认证层在经过明确所有权证明后，把已有 Actor 关联到真实 user_id。
- 不回写或迁移每一条历史业务记录；资源仍以 actor_id 为稳定所有权锚点。
- Task 查询、Conversation 查询和 Asset 下载必须先验证当前 ActorContext 是否拥有目标 actor_id；未来可通过 Actor.user_id 识别已认领身份。
- 认证令牌、密码、角色矩阵、多租户、project_id 和 tenant_id 不进入本节。
- 已确认一个 user_id 可以关联多个既有 actor_id，Actor.user_id 不设唯一约束。授权查询需要解析“该用户已安全认领的 Actor 集合”。

### 11.2 未来上传资产边界

- Asset 始终属于一个 Actor 和 Task，不要求一定有 producer ToolRun。
- 当前 MVP 只创建 source_type=GENERATED 的 Asset，并要求 producer_tool_run_id 非空。
- 未来 source_type=UPLOADED 时 producer_tool_run_id 为空，不需要伪造 ToolRun。
- 本轮不实现上传 API、Upload 实体或 ToolRunInputAssetLink。
- 未来 Tool 需要消费上传资产时，可以新增轻量输入资产关联；Asset 主体、结果资产关系和所有权字段不需要修改。
- 当前 ResultAssetLink 只接受 producer_tool_run_id 等于 Result.tool_run_id 的 AVAILABLE 生成资产。

### 11.3 新增 Tool

- Tool Registry 继续是代码级静态配置，不建立 Tool 数据库注册表。
- 新 Tool 继续复用 Task、TaskInputRevision、ToolRun、Asset、ToolResult 和 Explanation。
- Tool 特有输入进入 TaskInputRevision.raw_input/normalized_input；实际执行载荷进入 ToolRun.execution_input，并由 schema_version 校验。
- Tool 特有结果进入 ToolResult.data/warnings/provenance/error JSONB。
- 只有真正跨 Tool 通用、经常查询且需要约束的字段才升级为公共普通列。
- 新 Tool 没有模型、GPU 或内部步骤时也不影响数据模型；diagnostics 可以为空或记录其他 step。
- model_bundle_id 可空，且不扩展为模型版本表。

### 11.4 SSE

- 不建立 Event、TaskEvent、Event Store 或进度历史表。
- 进度仍由 TaskProgressReporter.report(task_id, tool_run_id, step, status, message, progress) 提供。
- SSE Adapter 未来只消费进度报告；断线、丢失或乱序不改变业务事实。
- Task、ToolRun、Asset、ToolResult、Explanation 与 LLMCall 是最终事实来源。
- 客户端重新连接时读取当前事实，而不是要求 MVP 事件重放。

## 12. 阶段 1A 待验证事项

以下只是在阶段 1A 实施前/中需要验证的逻辑门槛，不是本轮实施计划：

1. PostgreSQL/ORM 只实现并验证两类高价值复合来源约束：Task selected 引用和 ResultAssetLink 生成资产来源；不把全部所有权重复列升级为复合候选键。
2. Task selected 引用与 ToolRun → Task 的循环插入顺序使用可空后更新或等价简单方式，不要求为了逻辑模型引入复杂迁移技巧。
3. 已确认的 PostgreSQL text array 及 requested/completed/failed 集合 CHECK 能正确映射。
4. 并发创建 revision、ToolRun attempt_no、Explanation attempt_no 时的锁粒度与唯一冲突恢复。
5. 同幂等 key 并发请求只能创建一个 Message/Revision/ToolRun/Explanation。
6. ResultAssetLink 能阻止 Result 引用不同 task_id 或不同 producer_tool_run_id 的 Asset。
7. GENERATED/UPLOADED 与 producer_tool_run_id 的行内 CHECK 生效；MVP 测试只创建 GENERATED。
8. Asset PENDING/AVAILABLE/FAILED/ORPHANED 的条件更新、stale 查询和 object_key 冲突恢复。
9. MinIO 成功但 AVAILABLE 提交失败、broken AVAILABLE reference 的恢复。
10. SHA-256 无索引时的实际恢复查询足够；只有真实慢查询才添加。
11. JSONB Schema 校验覆盖 TaskInputRevision、ToolRun.execution_input/diagnostics、ToolResult 和 LLM structured_output_summary。
12. INTENT_AND_PARAMETER_EXTRACTION LLMCall 能记录安全候选摘要，并通过 source_llm_call_id 区分提取错误与 Application 校验错误。
13. Message/TaskInputRevision/ToolRun/LLMCall.request_id 能与结构化日志串联，且在无实际查询需求时不建立额外索引。
14. Task 不含 duration_ms；ToolRun、LLMCall、Explanation 耗时与 Task 墙钟时间语义清晰。
15. 不保存完整 Prompt，只保存已确认的模板身份/版本、digest、输入引用、模型、参数、usage 和安全摘要。
16. 一个 user_id 关联多个 actor_id 时的安全认领与访问控制查询。
17. 数据保留、硬删除和 MinIO 删除补偿在正式登录/合规设计前保持 RESTRICT。
18. ID 的物理类型、时区时间类型、digest 存储类型等在实施前统一，但不改变本文逻辑身份。

## 13. 验收场景

1. 新匿名 Actor 可创建 Conversation、User Message 和 Task，所有权由普通外键与创建事务保持一致。
2. 一个未来 user_id 可以关联多个 actor_id，Actor.user_id 不设唯一约束。
3. Conversation 消息能按 created_at + message_id 稳定排序。
4. Message、TaskInputRevision、ToolRun 和 LLMCall 保存 request_id，且不建立 Request 表。
5. 自然语言输入先创建 purpose=INTENT_AND_PARAMETER_EXTRACTION 的 LLMCall。
6. 意图调用的 structured_output_summary 能表达候选意图、tool_id、值/单位、requested outputs 和 missing/ambiguous fields，但不保存完整 Prompt/provider 响应。
7. 自然语言形成的 TaskInputRevision 可以通过 source_llm_call_id 追溯意图提取调用。
8. 直接结构化请求或确定性补充可以令 source_llm_call_id 为空。
9. LLM 候选不能直接成为执行事实；TaskInputRevision 保存 Application 处理后的 raw_input/normalized_input。
10. 普通知识问答使用意图提取与知识回答 LLMCall，只创建 AssistantMessage，不创建 ToolRun/Asset/ToolResult。
11. LLM 失败时不创建伪造的 Revision 或 AssistantMessage，并保存安全错误。
12. 缺少输入时 Task 为 NEEDS_INPUT，不创建 ToolRun。
13. 补充输入保留 task_id，追加 Message 与 TaskInputRevision，不覆盖旧快照。
14. 同一补充幂等 key/digest 重放不追加 Message 或 Revision。
15. 每次实际 Tool 执行创建新 tool_run_id 和 Task 内唯一 attempt_no。
16. ToolRun 通过 task_input_revision_id 追溯输入，只保存 execution_input 和可空 actual_runtime_parameters，不再保存 input_snapshot/normalized_input。
17. execution_input 与实际传给 MaterialTool.execute 的载荷一致。
18. 当前 SEM Asset 的 source_type=GENERATED 且 producer_tool_run_id 必填。
19. 数据模型允许未来 source_type=UPLOADED 且 producer_tool_run_id 为空，但 MVP 不实现上传 API、Upload 实体或 ToolRunInputAssetLink。
20. Actor/Task 可以拥有 Asset，ToolRun 只产生 0..N GENERATED Asset。
21. ResultAssetLink 复合来源约束阻止不同 Task/ToolRun 的生成 Asset 与 Result 混合。
22. Task selected_tool_run_id 和 selected_result_id 必须指向同一来源链。
23. 只请求性能时，中间 SEM 仍保存为 producer_tool_run_id 指向本次 ToolRun 的 Asset。
24. 中间 SEM AVAILABLE 而性能失败时，completed_outputs 仍为空且 Task FAILED。
25. Asset 非 AVAILABLE 时不能建立成功 ResultAssetLink。
26. object_key 全局唯一；API 投影不暴露 object_key。
27. requested/completed/failed outputs 使用 PostgreSQL text array。
28. 一个 ToolRun 最多产生一个 ToolResult。
29. ToolResult 通过 ResultAssetLink 组装 artifacts，不把 asset_id 外键藏在 JSONB。
30. ToolResult 成功而 Explanation 失败时，ToolRun/ToolResult 不改写，Task 部分成功。
31. Explanation 重试引用同一 result_id，创建新的 explanation_id 和 llm_call_id。
32. 不增加 selected_explanation_id；同一 Result/语言按 completed_at DESC、explanation_id DESC 读取最近一次成功解释。
33. Task 只有 created_at/started_at/updated_at/completed_at，不保存 duration_ms；执行耗时保存在 ToolRun/LLMCall/Explanation。
34. 数据库直接保证幂等、revision、attempt_no、object_key、单 Result、ResultAssetLink 来源和 selected 来源等高价值不变量。
35. 重复所有权、LLM conversation、版本快照等一致性由 Application 短事务校验，不要求多层复合候选键。
36. 整体 Tool 重试保留 task_id，新建 request_id/ToolRun/Asset/Result，旧事实不可变。
37. 结构化日志和 TaskProgressReporter 不进入业务表。
38. 不存在 model_version、InferenceRun、StageRun、PredictorRawOutput、Event 或状态历史实体。
39. 新增第二个不同类型 Tool 不需要新增一套 Task/ToolRun/Asset/ToolResult 表。
40. 未来 SSE 读取 TaskProgressReporter，最终状态仍从核心实体查询。
41. 数据库事务不跨 Runtime、MinIO 或 LLM 网络调用。

## 14. 已确认设计决定

### 14.1 Actor 与未来 user_id 的基数

已确认：一个未来 user_id 可以关联多个已安全认领的 actor_id；Actor.user_id 不设唯一约束。授权层解析该用户已认领的 Actor 集合，不迁移历史资源所有权。

### 14.2 outputs 的物理表示

已确认：requested_outputs、completed_outputs、failed_outputs 使用 PostgreSQL text array，配合应用契约和简单 CHECK。

### 14.3 ToolResult artifacts 的关系表示

已确认：保留 ResultAssetLink。它直接保证生成 Asset 与 Result 的 Task/ToolRun 来源一致，并支持一个 Tool 返回多个文件。

### 14.4 LLM Prompt 留存

已确认：MVP 不保存完整 Prompt，只保存输入引用、模板身份/版本、prompt digest、generation parameters、模型标识、usage、耗时、structured output 安全摘要和安全错误。未来如确有科研复现需求，必须另行设计脱敏、保留期和访问控制，不能改变本基线的 MVP 默认。

### 14.5 Explanation 选择

已确认：MVP 不增加 selected_explanation_id。展示层对同一 result_id 和 language 过滤 SUCCEEDED，并按 completed_at DESC、explanation_id DESC 确定性读取最近一次成功解释；ToolResult 始终是事实来源。

本节没有待项目负责人确认的遗留设计选择。

## 15. 一致性与结束边界

本文没有重新引入：

    model_version
    InferenceRun / inference_run_id
    StageRun 独立实体
    PredictorRawOutput 独立实体
    Event / Event Store
    状态转换历史表
    TaskExecutor
    Observability / Trace / Span 业务模型
    Tool 数据库注册表
    模型版本表

本文继续保留并具体化：

    ActorContext(actor_id, user_id)
    Conversation / Message / Task
    TaskInputRevision / IdempotencyRecord
    Task → 多个 ToolRun
    Message / TaskInputRevision / ToolRun / LLMCall request_id
    INTENT_AND_PARAMETER_EXTRACTION LLMCall
    ToolRun.execution_input
    ToolRun diagnostics JSONB
    Asset producer_tool_run_id 可空
    Asset PENDING / AVAILABLE / FAILED / ORPHANED
    ToolResult / NaturalLanguageExplanation / LLMCall
    ResultAssetLink
    Task selected_tool_run_id / selected_result_id
    TaskProgressReporter 非持久化边界
    新 Tool、SSE 和登录系统的最小兼容边界

本文未发现需要修改第一节或第二节已确认基线正文的真实逻辑冲突。第三节已完成简化和补充并确认为“已确认设计基线”；这不表示进入阶段 1。
