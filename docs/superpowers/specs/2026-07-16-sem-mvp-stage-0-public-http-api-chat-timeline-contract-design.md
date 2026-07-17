# 材料智能体平台阶段 0 第四节 A：公共 HTTP API 与聊天时间线契约设计

> 状态：已确认设计基线
>
> 确认日期：2026-07-16
>
> 前置基线：第一节、第二节和第三节均为“已确认设计基线”
>
> 适用范围：平台对前端和未来外部调用者提供的公共 HTTP API
>
> 明确排除：本地 ZTA35G Tool Runtime 的 HTTP/RPC 通信协议，该部分由独立第四节 B 定义
>
> 事实依据：本轮重新检查仓库状态并完整读取第一节、第二节、第三节和第四节 A 最新文档；本轮只对第四节 A 做小范围修订并新增第四节 B，不修改第一、第二、第三节正文或 `SEM/`，不运行模型、不加载权重、不安装依赖，也不创建任何 API、Schema、数据库或业务代码。

## 项目负责人审阅层

本节解决的是：前端或未来外部调用者怎样通过统一的 `/api/v1` 接口创建对话、提交问题、查看任务和结果、补充缺失参数、重试失败环节，以及安全读取图片。它只描述用户能够看到的产品行为，不设计后台本地模型进程怎样通信。

### 1. 用户可以执行哪些操作

用户可以新建对话、查看对话列表、提交知识问题或 Tool 请求、查看统一聊天历史、查看 Task 当前状态、为 `NEEDS_INPUT` Task 补充参数、显式重试 Tool、单独重试结果解释、查看结构化结果、查看或下载图片、查看当前可用 Tool，以及检查平台和依赖是否可用。

MVP 不提供 Tool 动态增删改、模型版本选择、真实 SEM 上传、SSE/WebSocket、注册登录、角色管理、任务队列或 Worker 管理接口。

### 2. 用户提交消息后会收到什么

用户先创建 Conversation，再向该 Conversation 提交一条消息。系统保存 UserMessage 和 Task，然后默认只进行一次聊天编排：

- 如果不需要 Tool，直接返回知识回答；
- 如果需要 Tool 且参数完整合法，执行 Tool 并返回 Task、结构化结果、图片引用和解释；
- 如果 Tool 意图明确但缺少或歧义，返回 `NEEDS_INPUT`、正式缺失字段和追问。

前端不直接看到 LLM 内部结构化输出。只有 Application 完成标准化、校验和持久化后，API 才返回正式 Task 状态和字段错误。

### 2.1 三个写入执行接口怎样返回

MVP 的写入执行接口采用同步、请求内执行：

```text
POST /api/v1/conversations/{conversation_id}/messages
POST /api/v1/tasks/{task_id}/tool-runs
POST /api/v1/tool-results/{result_id}/explanations
```

正常情况下，这些请求要等到本次操作形成稳定业务状态后才返回：

```text
NEEDS_INPUT
SUCCEEDED
PARTIALLY_SUCCEEDED
FAILED
```

MVP 不返回 `202 Accepted` 后依赖未设计的 Worker、队列或后台任务继续执行，也不使用 FastAPI 简单 BackgroundTask 承担可靠模型执行。`PENDING/RUNNING` 可以被 `GET /api/v1/tasks/{task_id}` 读取，例如另一个请求正在执行或客户端并发查询当前事实，但不是上述三个 POST 的正常最终响应。

### 3. 缺参数时前端怎样显示和补充

前端在 Task 卡中显示 `NEEDS_INPUT`、追问文本、`missing_fields`、`ambiguous_fields` 和已经确认的输入摘要。用户补充时仍通过同一个消息入口提交，但必须明确带上目标 `task_id`。如果对话中有多个等待补充的 Task，系统绝不猜测用户在补哪一个。

补充会追加新的 UserMessage 和输入 revision，不覆盖旧消息或旧输入快照。补充后仍缺参数则继续 `NEEDS_INPUT`；完整合法才执行 Tool；完整但非法则直接失败。

### 4. 成功、部分成功和失败在界面上的区别

- `SUCCEEDED`：用户请求的正式结果已经可靠保存。
- `PARTIALLY_SUCCEEDED`：至少有一项请求输出可用，或结构化 ToolResult 可用但 Explanation 失败。界面必须保留可用结果，并明确显示失败部分。
- `FAILED`：用户目标没有可用请求输出，或必要资产/结构化结果没有可靠提交。
- `NEEDS_INPUT`：还没有执行 Tool，等待用户补充。

这些是 Task 业务状态，不等同于 HTTP 状态。`NEEDS_INPUT` 和有可用结果的 `PARTIALLY_SUCCEEDED` 使用正常 HTTP 200，不使用 HTTP 206。

Chat Orchestration 或 Tool Runtime 超时会被记录为规范化失败，当前不新增 `TIMED_OUT` Task/ToolRun 状态。Chat Orchestration 超时使 Task `FAILED`；Tool Runtime 超时使 ToolRun/Task `FAILED`；公共 HTTP 可以返回 `504`，并使用 `UPSTREAM_TIMEOUT`、`TOOL_RUNTIME_TIMEOUT` 或等价清晰的安全错误码。任何未持久化的内存结果都不能作为成功响应返回。

若 ToolResult 已经成功，而 Explanation 超时，ToolResult 和可用 Asset 保持成功，Task 为 `PARTIALLY_SUCCEEDED`，HTTP 为 `200`，响应同时返回结构化结果和 Explanation 超时错误，不丢弃已成功结果。

客户端连接断开不等于用户主动取消。MVP 不提供取消 API，也不承诺请求断开后一定可靠继续执行。客户端无法确定原请求结果时必须复用同一 `Idempotency-Key`；重放读取原资源当前事实，不重复创建 Message、ToolRun 或 Explanation。

### 5. 如何查看历史任务和历史结果

对话时间线把知识问答和 Tool 任务放在同一历史中：

- 知识回答显示为独立 AssistantMessage；
- Tool 请求显示为一个 Task 卡；
- Task 卡中包含初始用户消息、当前状态、选中的 ToolRun、历史 ToolRun 摘要、ToolResult、图片、Explanation 和错误。

Task 卡的位置由初始 UserMessage 的时间决定，找不到时才回退到 Task 创建时间。Tool 重试、Explanation 重试或状态更新只改变卡片内部内容，不会把旧卡片移动到对话底部。

### 6. Tool 重试与 Explanation 重试的区别

Tool 重试会创建新的 ToolRun，并产生自己的图片、结构化结果和 diagnostics。旧 ToolRun 和旧结果全部保留，不能覆盖或混合。

Explanation 重试只对同一个 ToolResult 再生成一份解释，创建新的 Explanation 和 LLMCall，不重新执行 Tool，也不重新生成图片或性能结果。

已成功的 Tool Task 如果用户希望按相同条件“再生成一次”，应创建新的 Task，而不是重开成功 ToolRun。

### 7. 图片怎样下载

前端先从 ToolResult 或 Asset 元数据中获得 `asset_id`，再调用平台受控内容接口读取图片。平台每次都先检查当前 Actor 是否拥有该 Asset，并确认状态为 `AVAILABLE`。

MVP 选择由应用接口转发图片，而不是返回永久公开 MinIO 地址。这样可以保持所有权检查一致，并隐藏 object key、bucket、密钥和宿主机路径。当前文件主要是较小的 512×512 PNG，这个方案足够简单。

### 8. 哪些内部信息不会暴露给用户

公共响应不暴露：

- MinIO object key、bucket、管理地址或密钥；
- 宿主机绝对路径和模型权重路径；
- 完整 Prompt、完整 LLM provider 响应或异常堆栈；
- Tensor、DenseNet 特征、SVR 中间值或模型激活；
- 本地 Runtime 的内部端口、传输协议、进程控制或图片内部序列化格式；
- `model_version` 或模型文件选择能力。

### 9. 登录系统和 SSE 以后怎样接入

MVP 由本地匿名上下文构造 ActorContext。所有 Conversation、Task、Result 和 Asset 查询已经经过相同所有权边界。未来登录系统只需由认证中间件构造真实 ActorContext，不需要改写资源 API 或迁移历史结果。

当前不创建 SSE 路径或事件协议。未来可以把 TaskProgressReporter 接到 SSE Adapter，向前端发送临时进度；最终状态仍从 Task 和时间线接口读取。断线后前端重新查询持久化事实，不要求事件重放。

### 10. 项目负责人如何人工验收

项目负责人可以使用浏览器或简单 HTTP 客户端按以下顺序验收：

1. 创建对话并提交知识问题，确认一次聊天编排直接形成 AssistantMessage。
2. 提交完整 Tool 请求，确认 Task、结果和图片可以查询。
3. 提交缺参数请求，确认显示 `NEEDS_INPUT`，补充时明确绑定原 Task。
4. 重复相同幂等请求，确认不重复创建 Message、Task、ToolRun 或 Explanation。
5. 触发部分成功和 Explanation 失败，确认可用结果仍显示。
6. 重试 Tool，确认旧运行保留；重试 Explanation，确认 Tool 不执行。
7. 更新旧 Task 状态后重新读取时间线，确认卡片位置不移动。
8. 尝试读取非 `AVAILABLE` 图片或其他 Actor 的资源，确认被拒绝。
9. 检查所有 JSON 响应，确认不出现 object key、bucket、内部路径或异常堆栈。
10. 确认没有 SSE、登录、上传、动态 Tool 管理或本地 Runtime 协议接口。
11. 确认三个写入执行 POST 在正常情况下返回稳定业务状态，不返回 `202`，不把 `PENDING/RUNNING` 当作正常最终响应。
12. 分别触发 Chat Orchestration、Tool Runtime 和 Explanation 超时，确认前两者形成规范化失败，Explanation 超时不丢弃已成功 ToolResult。
13. 在请求结果不确定时复用同一 `Idempotency-Key`，确认只读取原资源当前事实，不重复创建 Message、ToolRun 或 Explanation。
14. 验证默认 Conversation 标题、图片 inline/attachment 策略，以及 `/health/ready` 只返回安全摘要。

## 1. 范围与非范围

### 1.1 本节目标

第四节 A 定义平台公共 HTTP 边界，回答：

- 用户和前端可以执行哪些操作；
- 请求输入、返回结构和资源标识怎样保持一致；
- 一次 CHAT_ORCHESTRATION 的三类结果怎样映射到公共响应；
- Conversation 统一时间线怎样聚合且稳定分页；
- Task、ToolRun、ToolResult 和 Asset 怎样查询；
- `NEEDS_INPUT`、部分成功和失败怎样表达；
- 三个写入执行 POST 怎样同步返回稳定业务状态；
- Chat Orchestration、Tool Runtime、Explanation 超时和客户端断开怎样形成公共行为；
- 幂等 key 怎样阻止重复写入；
- 当前匿名 Actor 与未来登录怎样共用所有权检查；
- 未来 SSE 怎样接入而不改变最终事实接口。

本文是契约设计，不给出 FastAPI、Pydantic、SQLAlchemy、Repository、DDL、migration 或前端实现。

### 1.2 本节明确非范围

本节不设计或实现：

- 本地 ZTA35G Tool Runtime 的 HTTP/RPC 路径、请求/响应、图片传输、超时、重试、进程启动、并发或错误协议；
- 第四节 B；
- FastAPI 路由、Pydantic Schema、OpenAPI 生成代码或 Repository；
- 数据库表、DDL、Alembic migration 或 SQLAlchemy Model；
- Tool、Runtime、Adapter、LangChain、前端或 Docker Compose；
- Redis、Worker、任务队列、事件总线或 Event Store；
- `202 Accepted` 后继续执行、FastAPI BackgroundTask 可靠执行或取消 API；
- SSE/WebSocket 路径和事件格式；
- 注册、登录、密码、JWT、OAuth、角色或多租户；
- 真实 SEM 上传；
- Tool 动态增删改、插件市场或数据库 Tool 注册表；
- 模型版本选择、模型发布、灰度或回滚；
- 阶段 1 实施计划、依赖安装、模型运行或 git commit。

### 1.3 第四节 A 与第四节 B 的分界

第四节 A 只看到：

```text
Application
→ MaterialTool / Tool Client Adapter
→ 返回 ToolExecutionOutput 等价业务结果
```

第四节 A 不规定 Adapter 后面使用 HTTP、RPC、标准输入输出、共享文件还是其他本地通信方式，也不规定内部图片字节怎样封装。公共健康接口可以显示某个 Tool 当前 `AVAILABLE / UNAVAILABLE / DEGRADED`，但不定义后台怎样探测本地 Runtime。

## 2. API 设计原则

### 2.1 核心原则

1. 所有公共业务路径使用 `/api/v1`。
2. 路径围绕 Conversation、Task、ToolResult、Asset 和 Tool Catalog 资源，不暴露数据库表名。
3. 对话消息只有一个公共写入口，避免“聊天提交”和“补充输入”形成两套重复语义。
4. Tool 重试创建新的 ToolRun；Explanation 重试创建新的 Explanation。
5. 查询响应只投影已持久化事实，不从 Runtime 内存或 LLM 临时输出直接返回成功结果。
6. Task 业务状态、ToolResult 状态和 HTTP 状态分别表达，不混为一套枚举。
7. 公共响应使用明确判别字段和有界子结构；Tool 特有 `data` 由 `schema_version` 约束，不接受任意无界 JSON。
8. 所有资源读取先经过 Actor 所有权边界。
9. 内部 object key、路径、密钥、堆栈和模型对象不得进入响应。
10. 当前不为未来 SSE、登录或远程 Tool Runtime 预建未使用的公共接口。
11. 三个写入执行 POST 采用同步、请求内执行，正常返回 `NEEDS_INPUT / SUCCEEDED / PARTIALLY_SUCCEEDED / FAILED` 稳定业务状态。
12. 查询可以读取并发执行中的 `PENDING/RUNNING`，但写接口不把它们描述为正常最终响应。
13. 超时和客户端断开只固定公共行为与幂等恢复，不在本节承诺可靠后台继续执行或实现取消系统。

### 2.2 路径风格比较

| 风格 | 示例 | 优点 | 问题 | 结论 |
|---|---|---|---|---|
| 动作式路径 | `/tasks/{id}:retry` | 语义直观 | 容易不断增加动作，资源关系不统一 | 不采用 |
| 通用命令入口 | `/commands` | 路径少 | 输入联合过大，权限、幂等和返回含义模糊 | 不采用 |
| 资源与子资源 | `/tasks/{id}/tool-runs`、`/tool-results/{id}/explanations` | 新尝试有明确资源身份，容易查询和幂等 | 需要解释 POST 的创建语义 | 采用 |

### 2.3 消息补充入口比较

| 方案 | 优点 | 问题 | 结论 |
|---|---|---|---|
| 另建 `/tasks/{id}/inputs` | 路径直观 | 同一用户文本可能走两个写入口，Message/Revision 幂等容易重复 | 不采用 |
| 所有文本进入 Conversation messages | 时间线和消息事实只有一个入口 | 请求体必须明确新 Task 或补充 Task | 采用 |

因此 `POST /conversations/{conversation_id}/messages` 同时承担新 Task 和 `NEEDS_INPUT` 补充。补充必须使用 `submission_mode=SUPPLEMENT_TASK` 并显式携带 `target_task_id`。

## 3. 公共资源与路径总览

### 3.1 推荐的 MVP 路径

| 方法 | 路径 | 能力 | MVP |
|---|---|---|---|
| POST | `/api/v1/conversations` | 创建 Conversation | 必需 |
| GET | `/api/v1/conversations` | 查询当前 Actor 的 Conversation 列表 | 必需 |
| GET | `/api/v1/conversations/{conversation_id}/timeline` | 查询统一聊天时间线 | 必需 |
| POST | `/api/v1/conversations/{conversation_id}/messages` | 提交新知识问答、新 Tool 请求或补充 NEEDS_INPUT | 必需 |
| GET | `/api/v1/tasks/{task_id}` | 查询 Task 当前状态、selected 结果和 ToolRun 历史摘要 | 必需 |
| POST | `/api/v1/tasks/{task_id}/tool-runs` | 显式创建新的 ToolRun 重试尝试 | 必需 |
| GET | `/api/v1/tool-results/{result_id}` | 查询完整 ToolResult 公共投影 | 必需 |
| POST | `/api/v1/tool-results/{result_id}/explanations` | 显式创建新的 Explanation 重试尝试 | 必需 |
| GET | `/api/v1/assets/{asset_id}` | 查询 Asset 公共元数据 | 必需 |
| GET | `/api/v1/assets/{asset_id}/content` | 受控读取或下载 AVAILABLE Asset | 必需 |
| GET | `/api/v1/tools` | 查询 Tool Catalog 列表 | 必需 |
| GET | `/api/v1/tools/{tool_id}` | 查询单个 Tool Catalog 条目 | 必需 |
| GET | `/api/v1/health/live` | 平台进程健康检查 | 必需 |
| GET | `/api/v1/health/ready` | 依赖与主要能力就绪检查 | 必需 |

### 3.2 本轮不创建或不预留的路径

不创建：

```text
/api/v1/admin/tools/*
/api/v1/models/*
/api/v1/uploads/*
/api/v1/auth/*
/api/v1/users/*
/api/v1/roles/*
/api/v1/sse
/api/v1/events
/api/v1/ws
/api/v1/workers/*
/api/v1/queues/*
/api/v1/runtime/*
/api/v1/zta35g-runtime/*
```

若未来确有上传、登录或 SSE 需求，应在对应设计中新增契约，不在当前响应中返回不可用链接。

## 4. 公共请求与响应通用约定

### 4.1 标识和时间

- ID 是不透明字符串，客户端不得解析前缀或推断顺序。
- 时间使用带时区的 ISO 8601/RFC 3339 字符串，公共响应统一为 UTC。
- 客户端不能使用 `updated_at` 重新推断聊天位置。
- 数值和单位按 Tool schema 返回，不允许客户端自行猜测单位。

### 4.2 公共响应的最小公共字段

JSON 业务响应至少包含：

```yaml
request_id: req_...
data: <当前端点的明确资源结构>
```

发生协议或操作错误时使用第 13 节统一错误包络。成功响应不强制返回空 `errors` 数组。

### 4.3 安全输出规则

所有公共投影必须过滤：

```text
object_key
bucket
storage endpoint
host absolute path
model weight path
secret / credential
full prompt
full provider response
internal stack trace
Tensor / feature / activation
internal Runtime transport detail
```

安全错误只返回稳定 `code`、用户可理解的 `message`、可选字段级 `details` 和已创建资源引用。

## 5. 消息提交与 CHAT_ORCHESTRATION

### 5.1 Conversation 选择

消息入口固定为：

```text
POST /api/v1/conversations/{conversation_id}/messages
```

`conversation_id` 必须来自路径，不从请求正文、最近访问记录或当前唯一 Conversation 推断。调用者若没有 Conversation，先调用 `POST /api/v1/conversations`。这样可以避免多标签页、多个对话或未来外部客户端把消息写入错误会话。

### 5.2 请求结构

```yaml
submission_mode: NEW_TASK | SUPPLEMENT_TASK
content_text: "用户原始文本"
target_task_id: task_... | null
```

规则：

- `NEW_TASK`：创建新的 UserMessage 和 Task；`target_task_id` 必须为空。
- `SUPPLEMENT_TASK`：补充既有 `NEEDS_INPUT` Task；`target_task_id` 必填。
- `target_task_id` 必须属于路径 Conversation 和当前 Actor。
- `SUPPLEMENT_TASK` 的目标状态必须是 `NEEDS_INPUT`。
- 同一请求不能既创建新 Task 又补充旧 Task。
- `content_text` 必须是非空用户文本；MVP 不在该入口接受图片上传或自由 JSON Tool 输入。

存在多个 `NEEDS_INPUT` Task 时，前端必须从对应 Task 卡携带明确 `target_task_id`。后端不依据“最近 Task”、文本相似度或 LLM 猜测绑定。

### 5.3 内部 Chat Orchestration 判别联合

Application 内部使用明确判别字段：

```yaml
route: KNOWLEDGE_ANSWER
answer_text: "..."
```

或：

```yaml
route: TOOL_EXECUTION
tool_id: zta35g_sem_virtual_lab
candidate_parameters: <受控候选结构>
requested_outputs: [sem_image, mechanical_properties]
```

或：

```yaml
route: NEEDS_INPUT
tool_id: zta35g_sem_virtual_lab
candidate_parameters: <受控候选结构>
missing_fields: [...]
ambiguous_fields: [...]
follow_up_suggestion: "..."
```

该联合是 Application 内部契约，不要求原样出现在公共 HTTP 响应中。`candidate_parameters`、`follow_up_suggestion` 和 LLM 自报错误都不是正式 API 事实。

### 5.4 Application 正式化边界

同一次 CHAT_ORCHESTRATION 完成默认路由：

```text
UserMessage + Task
→ LLMCall(CHAT_ORCHESTRATION)
→ 事务外调用一次 LLM
→ 三类内部 route 之一
→ Application 标准化、单位转换、完整校验和 Tool Registry 解析
→ 保存正式 Message / TaskInputRevision / Task 状态
→ API 返回正式事实
```

公共响应中的以下字段只能由 Application 生成：

```text
task_type
task_status
missing_fields
ambiguous_fields
validation_errors
tool_id
requested_outputs
selected_tool_run_id
selected_result_id
```

LangChain/LLM 的原始 structured output、完整 Prompt、provider 响应和候选错误不得直接透传。

### 5.5 三类内部结果到公共响应的映射

#### KNOWLEDGE_ANSWER

```text
Task.task_type = KNOWLEDGE_QA
Task.status = SUCCEEDED
AssistantMessage 必填
ToolRun / ToolResult / Asset = 不存在
HTTP = 200
```

#### TOOL_EXECUTION

Application 完成标准化和校验：

- 完整合法：执行 Tool，并返回 Task 当前状态、selected ToolRun/Result 摘要；
- 完整但非法：Task `FAILED`，返回 HTTP 422 和正式 validation errors；
- Tool/Runtime 或持久化失败：按第 13 节映射。

#### NEEDS_INPUT

```text
Task.task_type = TOOL_EXECUTION
Task.status = NEEDS_INPUT
needs_input.missing_fields / ambiguous_fields 必填
AssistantMessage 追问可显示
ToolRun = 不存在
HTTP = 200
```

### 5.6 消息提交成功响应

```yaml
request_id: req_...
data:
  conversation_id: conv_...
  user_message:
    message_id: msg_...
    role: USER
    content_text: "..."
    created_at: "..."
  task:
    task_id: task_...
    task_type: KNOWLEDGE_QA | TOOL_EXECUTION | null
    status: PENDING | RUNNING | NEEDS_INPUT | SUCCEEDED | PARTIALLY_SUCCEEDED | FAILED
    selected_tool_run_id: trun_... | null
    selected_result_id: res_... | null
  assistant_message: <知识回答或追问 MessageView，允许为空>
  needs_input: <NeedsInputView，非 NEEDS_INPUT 时为空>
  result_summary: <ToolResultSummary，尚无结果时为空>
  explanation: <ExplanationView 或状态摘要，允许为空>
  idempotency_replayed: false
```

这不是把 ToolResult 复制进 AssistantMessage；`result_summary` 是查询投影，正式事实仍在 ToolResult、Asset 和 Explanation。

正常的消息写入请求必须在 Task 达到 `NEEDS_INPUT / SUCCEEDED / PARTIALLY_SUCCEEDED / FAILED` 后返回。上例保留完整 Task 状态枚举，是因为幂等重放可能读取到另一个仍在执行的原请求当前事实；`PENDING/RUNNING` 不是本 POST 的正常最终响应。该端点不返回 `202`，也不把后续可靠执行委托给 Worker、队列或 BackgroundTask。

## 6. Conversation 与统一聊天时间线

### 6.1 创建 Conversation

```text
POST /api/v1/conversations
```

请求：

```yaml
title: "可选展示标题"
```

响应：

```yaml
request_id: req_...
data:
  conversation_id: conv_...
  title: "..."
  created_at: "..."
  updated_at: "..."
```

成功使用 HTTP 201。MVP 不在创建 Conversation 时额外调用 LLM 生成标题。

标题规则已经确认：

1. 调用者可以提供可选标题。
2. 未提供标题时，Conversation 可以先以空标题创建；首条 UserMessage 成功保存后，使用确定性首消息截断生成默认标题。
3. 默认标题生成不调用标题专用 LLM，不影响消息提交成功，也不改变 Task 业务状态。
4. 具体截断长度、字符清理和并发更新规则留到阶段 1A 验证。
5. 首消息为空白或清理后无法形成标题时使用固定默认值，例如“新对话”。

### 6.2 Conversation 列表

```text
GET /api/v1/conversations?limit=...&cursor=...
```

每项至少包含：

```text
conversation_id
title
created_at
updated_at
last_activity_preview
```

列表按 `updated_at DESC, conversation_id` 稳定排序。Conversation 列表允许因新消息而移动；该规则不同于聊天时间线内 Task 卡的稳定位置。

### 6.3 TimelineItem 判别联合

顶层 TimelineItem 只允许：

```text
USER_MESSAGE
ASSISTANT_MESSAGE
TOOL_TASK
```

共同字段：

```yaml
item_type: USER_MESSAGE | ASSISTANT_MESSAGE | TOOL_TASK
item_id: "message_id；TOOL_TASK 使用 task_id"
task_id: task_...
anchor_at: "用于顶层排序的时间"
```

每种 item 使用明确结构，不使用任意 `payload` 大对象：

- `USER_MESSAGE`：`message` 必填；
- `ASSISTANT_MESSAGE`：`message` 必填；
- `TOOL_TASK`：`initial_user_message`、`task`、`tool_runs`、`result`、`assets`、`explanation`、`needs_input` 和 `errors` 使用各自受控结构。

### 6.4 Tool Task 聚合规则

Tool 路径以一个 `TOOL_TASK` 项聚合：

```text
initial UserMessage
└─ Task 当前状态
   ├─ selected ToolRun
   ├─ 历史 ToolRun 摘要
   ├─ ToolResult
   ├─ Asset 引用
   ├─ NaturalLanguageExplanation
   ├─ NEEDS_INPUT prompt / supplement messages
   └─ 错误或部分成功信息
```

初始 UserMessage 不再作为另一个顶层 `USER_MESSAGE` 重复显示。补充 UserMessage 和追问 AssistantMessage 保持 Message 事实，但在该 Task 项的 `input_thread` 中展示。知识问答的 UserMessage 和 AssistantMessage 继续作为独立顶层消息项。

### 6.5 稳定锚点和排序

Tool Task 的 `anchor_at`：

```text
优先：发起该 Task 的初始 UserMessage.created_at
回退：Task.created_at
```

禁止使用 Task.updated_at、ToolRun.completed_at、Result.created_at 或 Explanation.completed_at 作为顶层锚点。Task 当前状态更新、Tool 重试、Explanation 重试或 selected 引用变化均不得改变 `anchor_at`。

顶层升序排序键为：

```text
(anchor_at, item_type_rank, item_id)
```

固定类型优先级：

```text
USER_MESSAGE = 10
ASSISTANT_MESSAGE = 20
TOOL_TASK = 30
```

Tool Task 内部：

- ToolRun：`created_at, attempt_no, tool_run_id`；
- ToolResult 和 Asset：跟随来源 ToolRun，不脱离来源单独排序；
- Explanation：在对应 Result 内按 `created_at, completed_at, explanation_id`；
- input thread：按 `Message.created_at, message_id`。

### 6.6 ToolRun 历史默认折叠

时间线默认只返回：

```text
attempt_count
selected_tool_run summary
has_history
```

历史 ToolRun 详情默认折叠，避免单个 Task 卡过大。用户展开时前端调用 `GET /tasks/{task_id}` 获取全部 ToolRun 摘要，不在时间线接口增加第二套完整运行数据。

### 6.7 ToolResult、Asset 和 Explanation 展示

- Timeline 中的 ToolResult 是受控摘要，并携带 `result_id` 供详情查询。
- Asset 默认作为 ToolResult 附件，不成为顶层 TimelineItem。
- Asset 引用只含公共元数据和受控内容 URL。
- Explanation 成功时显示最近一次成功解释。
- Explanation 最新尝试失败时显示安全错误；若此前已有成功解释，保留原成功文本并同时显示“最新重试失败”，不得让成功解释消失。
- ToolResult/Explanation 不复制为 Message。

### 6.8 分页游标

```text
GET /api/v1/conversations/{conversation_id}/timeline?limit=50&cursor=...
```

游标是不透明字符串，至少编码上一项的：

```text
anchor_at
item_type_rank
item_id
```

响应：

```yaml
request_id: req_...
data:
  conversation:
    conversation_id: conv_...
    title: "..."
  items: [TimelineItem...]
  next_cursor: "..." | null
  has_more: true | false
```

Task 内容可以更新，但稳定 `anchor_at` 不变，因此旧 Task 不会跨分页边界移动。游标具体编码、签名和最大 page size 留到阶段 1A 验证。

## 7. Task、ToolRun 查询与重试

### 7.1 Task 详情最小字段

```text
task_id
conversation_id
task_type
status
created_at
anchor_at
started_at
updated_at
completed_at
selected_tool_run_id
selected_result_id
error
needs_input
tool_run_count
tool_runs
selected_result_summary
explanation_summary
```

`anchor_at` 是查询投影，不要求新增数据库列。Task 详情不返回 actor_id、内部锁信息或数据库版本号。

### 7.2 ToolRun 历史摘要

每个 ToolRun 摘要至少包含：

```text
tool_run_id
attempt_no
tool_id
tool_version
schema_version
status
requested_outputs
completed_outputs
failed_outputs
created_at
started_at
completed_at
duration_ms
diagnostics_summary
error
is_selected
```

`diagnostics_summary` 只含安全的 step、status、duration 和错误摘要，不返回模型内部对象、绝对路径或 Runtime 传输细节。

### 7.3 显式 Tool 重试

```text
POST /api/v1/tasks/{task_id}/tool-runs
Idempotency-Key: <opaque key>
```

请求体可以为空，或只包含受控用户原因：

```yaml
reason: USER_REQUESTED_RETRY
```

规则：

1. 使用该 Task 已确认的最新合法 TaskInputRevision，不接受通过重试接口修改工艺参数。
2. 新建 ToolRun 和 attempt_no；旧 ToolRun、Asset、Result、diagnostics 和错误不可变。
3. Task 只在新结果提交后更新 selected 引用。
4. `NEEDS_INPUT` Task 不允许 Tool 重试，应先补充输入。
5. 完整非法输入导致的 FAILED Task 不允许 Tool 重试，应创建新的修正 Task。
6. 已成功 Task 的“重新生成”必须通过消息入口创建新 Task。
7. 同 key 同请求返回原 ToolRun；异摘要返回 409。

该 POST 在当前 HTTP 请求中同步创建并执行新的 ToolRun，直到形成 `SUCCEEDED / PARTIALLY_SUCCEEDED / FAILED` 稳定业务状态后返回。它不返回 `202`，不把 `PENDING/RUNNING` 作为正常最终响应，也不在请求结束后依赖未设计的 Worker、队列或 BackgroundTask。成功响应返回 Task 详情和新 `tool_run_id`；规范化、已持久化的 Tool 业务失败可以通过 HTTP 200 表达，基础设施不可用和超时按第 13 节返回。

### 7.4 Explanation 重试

```text
POST /api/v1/tool-results/{result_id}/explanations
Idempotency-Key: <opaque key>
```

请求：

```yaml
language: zh-CN
reason: USER_REQUESTED_RETRY
```

规则：

1. 固定引用该 `result_id`。
2. 新建 Explanation 和 LLMCall。
3. 不创建 ToolRun，不调用 Runtime，不重新保存 Asset。
4. 不覆盖旧 Explanation。
5. 同 key 同请求返回原 Explanation；异摘要返回 409。
6. 如果已有成功 Explanation，MVP 默认不把“换一种说法”当作 retry；需要重新生成文案时应在未来另行定义产品行为。

该 POST 在当前 HTTP 请求中同步等待本次 Explanation 达到 `SUCCEEDED` 或 `FAILED`。若 Explanation 超时或失败，而输入 ToolResult 已经成功，则 HTTP 返回 `200`，Task 为 `PARTIALLY_SUCCEEDED`，结构化结果和 Asset 继续返回；不得因为解释失败或超时丢弃 ToolResult。该端点不返回 `202`，也不在 BackgroundTask 中可靠继续解释。

## 8. ToolResult 查询契约

### 8.1 路径

```text
GET /api/v1/tool-results/{result_id}
```

### 8.2 公共 ToolResult

```yaml
result_id: res_...
task_id: task_...
tool_run_id: trun_...
status: SUCCEEDED | PARTIALLY_SUCCEEDED | FAILED
requested_outputs: [...]
completed_outputs: [...]
failed_outputs: [...]
data: <由 schema_version 约束的 Tool 数据>
artifacts: [AssetReference...]
warnings: [Warning...]
tool_id: zta35g_sem_virtual_lab
tool_version: 0.1.0
schema_version: "1.0"
provenance:
  task_id: task_...
  tool_run_id: trun_...
  input_revision: 1
  normalized_process_parameters: <允许用户查看的规范化参数>
  actual_runtime_parameters: <允许公开的安全运行参数>
error: <PublicErrorDetail | null>
created_at: "..."
```

`data` 不是任意无界 JSON；其形状由 `tool_id + schema_version` 约束。API 层对最大深度、最大数组长度和最大响应体设置保护，具体阈值留阶段 1A。

### 8.3 provenance 过滤

普通用户可以看到能够解释结果来源的：

```text
task_id
tool_run_id
input_revision
normalized_process_parameters
safe actual_runtime_parameters
generated asset references
tool_version
schema_version
```

不返回：

```text
model_bundle_id
weight file fingerprint
weight path
compatibility record internal path
Runtime address
internal adapter configuration
```

如未来需要受控运维视图，应设计独立授权边界，不能把内部 provenance 混进当前公共响应。

## 9. Asset 元数据与下载

### 9.1 Asset 公共元数据

```text
GET /api/v1/assets/{asset_id}
```

响应至少包含：

```yaml
asset_id: ast_...
task_id: task_...
status: PENDING | AVAILABLE | FAILED | ORPHANED
asset_type: sem_image
source_type: GENERATED
role: requested_output | intermediate | supporting
mime_type: image/png | null
width: 512 | null
height: 512 | null
bit_depth: 8 | null
byte_size: 12345 | null
sha256: "..." | null
created_at: "..."
available_at: "..." | null
download_url: "/api/v1/assets/ast_.../content" | null
error: <安全状态错误 | null>
```

只有 `AVAILABLE` 返回 `download_url`。公共响应不包含 object key、bucket、宿主机路径或永久公开 URL。

### 9.2 下载方案比较

| 方案 | 优点 | 主要问题 | 结论 |
|---|---|---|---|
| 永久公开 URL | 前端最简单 | 失去所有权检查，泄漏存储结构 | 拒绝 |
| 短期签名 URL | 应用不转发大文件 | 需要正确处理签名、过期和存储端暴露；当前收益有限 | 未来可选 |
| 应用受控内容端点 | 每次统一检查 Actor，隐藏 MinIO，错误语义一致 | 应用需要流式转发文件 | MVP 采用 |

当前 SEM PNG 较小，MVP 采用：

```text
GET /api/v1/assets/{asset_id}/content
```

### 9.3 受控内容端点规则

1. 先解析当前 ActorContext。
2. 校验 Asset 属于当前 Actor 的可访问 Task。
3. 校验 `status=AVAILABLE`。
4. 通过 StorageService 读取对象，API 不接收 object key。
5. 返回正确 `Content-Type`。
6. 文件名由 `asset_id + 安全扩展名` 或受控展示名生成，过滤路径分隔符和控制字符。
7. 图片默认使用 `Content-Disposition: inline`；调用者可使用受控查询参数请求 attachment。
8. 不返回永久签名 URL。
9. 非 AVAILABLE 返回 409 `ASSET_NOT_AVAILABLE`。
10. 对象缺失或校验不一致时不返回损坏内容，并触发安全错误/恢复记录。

未来当资产明显变大或下载吞吐成为真实瓶颈时，可以改为经过所有权检查后签发短期 URL；Asset 元数据和所有权契约不变。

## 10. Tool Catalog

### 10.1 路径

```text
GET /api/v1/tools
GET /api/v1/tools/{tool_id}
```

### 10.2 公共字段

```text
tool_id
display_name
description
material_scope
enabled
availability
tool_version
schema_version
supported_outputs
input_fields
output_summary
supported_asset_types
limitations
```

当前 ZTA35G Catalog 必须明确：

- 只适用于 ZTA35G 钛合金；
- 四维工艺参数和单位/范围；
- 支持 `sem_image` 与 `mechanical_properties`；
- 不支持上传真实 SEM 后预测；
- 只请求性能时仍会生成并保存中间 SEM；
- Tool 当前是否可用。

Catalog 是代码级 Tool Registry 的只读投影，不建立动态管理 API，也不返回模型文件、model version 或 Runtime 地址。

## 11. 健康检查

### 11.1 平台进程健康

```text
GET /api/v1/health/live
```

只回答 API 进程能否接受请求，不主动执行模型、不依赖 MinIO/PostgreSQL 成功，也不加载权重。

```yaml
status: LIVE
checked_at: "..."
```

### 11.2 依赖与能力就绪

```text
GET /api/v1/health/ready
```

返回安全组件名和状态：

```yaml
status: READY | DEGRADED | NOT_READY
components:
  - name: postgresql
    status: AVAILABLE | UNAVAILABLE
  - name: object_storage
    status: AVAILABLE | UNAVAILABLE
  - name: llm_provider
    status: AVAILABLE | UNAVAILABLE | UNKNOWN
  - name: zta35g_sem_virtual_lab
    status: AVAILABLE | UNAVAILABLE | DEGRADED
checked_at: "..."
```

规则：

- 核心数据库不可用时 `NOT_READY`，HTTP 503。
- 非核心能力局部不可用但平台仍可回答部分请求时 `DEGRADED`，HTTP 200。
- 全部所需能力正常时 `READY`，HTTP 200。
- 不返回连接字符串、主机名、端口、密钥、堆栈、权重路径或内部探测协议。
- 该端点不规定本地 Runtime 健康检查怎样实现，具体留第四节 B。
- 本地 MVP 可以直接访问上述安全摘要。
- 正式公网部署时默认限制为运维网络或已授权访问；认证、反向代理和网络访问限制留给部署/登录设计。

## 12. 幂等

### 12.1 Header 与请求体比较

| 位置 | 优点 | 问题 | 结论 |
|---|---|---|---|
| 请求体 `idempotency_key` | 容易在 JSON 中看到 | 污染每个业务 Schema，二进制或未来非 JSON 操作不统一 | 不采用 |
| HTTP Header `Idempotency-Key` | 所有写操作统一，业务正文保持纯净 | 客户端必须正确保存 Header | 采用 |

### 12.2 适用操作

以下操作必须携带：

```text
Idempotency-Key
```

| 操作 | 端点 | 幂等 operation |
|---|---|---|
| 创建消息和新 Task | POST Conversation messages + NEW_TASK | TASK_CREATE |
| 补充 NEEDS_INPUT | POST Conversation messages + SUPPLEMENT_TASK | TASK_INPUT_SUPPLEMENT |
| Tool 重试 | POST Task tool-runs | TOOL_RETRY |
| Explanation 重试 | POST Result explanations | EXPLANATION_RETRY |

GET 不使用幂等 key。创建空 Conversation 在 MVP 不强制该 Header，避免扩展第三节现有 IdempotencyRecord operation；若阶段 1A 发现真实重复创建问题，再单独评估。

### 12.3 摘要和重放规则

幂等作用域：

```text
actor_id + operation + Idempotency-Key
```

规范请求摘要至少包含：

- 路径资源 ID；
- submission_mode；
- target_task_id；
- 规范化的业务请求体；
- Explanation language；
- Tool/Explanation retry 操作类型。

不包含 request_id、接收时间或幂等 key 本身。

规则：

1. 同 key + 同规范请求摘要：返回第一次创建的资源当前事实。
2. 同 key + 不同摘要：HTTP 409 `IDEMPOTENCY_CONFLICT`。
3. 重放不得重复追加 Message、TaskInputRevision、ToolRun 或 Explanation。
4. 每次 HTTP 重放仍生成新的 request_id，响应标记 `idempotency_replayed=true`。
5. LLM 不参与摘要等价判断。
6. Tool 重试产生新 ToolRun，但同一 Tool 重试 key 重放只能指向同一个新 ToolRun。
7. Explanation 重试产生新 Explanation，但同一 key 重放只能指向同一个 Explanation。

### 12.4 客户端断开与结果不确定

1. 客户端连接断开不等于用户主动取消；MVP 不提供取消 API。
2. 客户端不知道原请求是否已经完成时，必须复用原 `Idempotency-Key`，不能生成新 key 猜测重试。
3. 同 key/同摘要重放读取第一次创建的资源当前事实，不重复追加 Message、TaskInputRevision、ToolRun 或 Explanation。
4. 若第一次请求仍在执行，重放或并发 GET 可能读取到 `PENDING/RUNNING`；这只是原资源当前事实，不是新 POST 的正常最终响应。
5. 实际断开时服务器执行是否继续、如何避免 ASGI/HTTP 客户端取消传播到关键持久化或 Tool 调用，由阶段 1A 实现验证。
6. 第四节 A 不承诺断开后可靠后台继续执行；Tool Runtime 可能仍在计算的风险和进程行为由第四节 B 进一步约束。

## 13. HTTP 状态、业务状态与错误包络

### 13.1 三层状态

```text
HTTP 状态
├─ 请求、认证、资源访问或基础设施结果
Task.status
└─ 用户目标当前业务状态
ToolResult.status
└─ 已持久化结构化 Tool 输出状态
```

不能用 HTTP 200 推断 Task 成功，也不能用 Task `PARTIALLY_SUCCEEDED` 推断网络传输不完整。

### 13.2 状态映射

| 场景 | HTTP | Task/Result 语义 |
|---|---:|---|
| 创建 Conversation | 201 | Conversation 已创建 |
| 知识回答成功 | 200 | Task SUCCEEDED |
| Tool 完整成功 | 200 | Task/ToolResult SUCCEEDED |
| NEEDS_INPUT | 200 | Task NEEDS_INPUT，无 ToolRun |
| 有可用结果的部分成功 | 200 | Task/ToolResult PARTIALLY_SUCCEEDED；不使用 206 |
| 已规范处理的 Tool 业务失败 | 200 | Task/ToolResult FAILED，错误已持久化 |
| 请求 JSON 无法解析 | 400 | 可不创建 Task |
| 公共请求字段不符合 Schema | 422 | 可不创建 Task |
| 输入完整但材料/单位/精度/范围/outputs 非法 | 422 | 已创建 Task FAILED，无 ToolRun |
| target_task_id 缺失、状态不允许或跨 Conversation | 409 或 422 | 不错误追加 Revision |
| 幂等 key 同 key 异摘要 | 409 | 原资源不变 |
| Asset 存在但非 AVAILABLE | 409 | 不返回内容 |
| 资源不存在或不属于当前 Actor | 404 | 不泄漏其他 Actor 资源是否存在 |
| 当前 Actor 已识别但未来权限策略禁止动作 | 403 | MVP 暂无角色策略 |
| 未来认证缺失或无效 | 401 | MVP 匿名 ActorContext 正常情况下不使用 |
| Tool/Runtime 已知不可用 | 503 | Task/ToolRun FAILED 或操作未开始 |
| PostgreSQL/MinIO 等必要依赖不可用 | 503 | 不误报成功 |
| Asset 保存或 ToolResult 提交发生内部故障 | 500 | Task FAILED；返回安全错误 |
| 未捕获 Tool/LLM 内部异常 | 500/502/503 | 按故障来源规范化，不返回堆栈 |
| Chat Orchestration 超时 | 504 | Task FAILED；`UPSTREAM_TIMEOUT` 或明确等价错误 |
| Tool Runtime 超时 | 504 | ToolRun/Task FAILED；`TOOL_RUNTIME_TIMEOUT` |
| ToolResult 可用但 Explanation 失败 | 200 | Task PARTIALLY_SUCCEEDED，返回结果与解释错误 |
| ToolResult 可用但 Explanation 超时 | 200 | Task PARTIALLY_SUCCEEDED，保留结果；`EXPLANATION_TIMEOUT` 或明确等价错误 |

### 13.3 同步执行与超时公共语义

```text
三个写入执行 POST
→ 在当前 HTTP 请求中执行
→ 形成稳定持久化业务状态
→ 返回响应
```

MVP 不使用 `202`、Worker、队列或 FastAPI BackgroundTask 承担这些操作的可靠完成。具体超时秒数不在第四节 A 固定：

- Chat Orchestration 超时：保存规范化失败记录，Task `FAILED`，不返回未持久化知识回答、候选参数或追问。
- Tool Runtime 超时：保存 ToolRun/Task 失败和安全错误，不返回未持久化内存图片或性能结果。
- Explanation 超时：若 ToolResult 已成功，保留 ToolResult/Asset，Task `PARTIALLY_SUCCEEDED`，HTTP `200`。
- 当前不新增 `TIMED_OUT` Task/ToolRun 状态；超时通过 `FAILED` 与稳定错误码表达。
- 公共超时响应可以使用 HTTP `504`；错误详情不得包含内部地址、客户端库异常或堆栈。

### 13.4 统一错误包络

```yaml
request_id: req_...
error:
  code: IDEMPOTENCY_CONFLICT
  message: "同一幂等键已用于不同请求。"
  details:
    - field: Idempotency-Key
      code: REQUEST_DIGEST_MISMATCH
      message: "请为新的业务操作使用新的幂等键。"
resource:
  conversation_id: conv_... | null
  task_id: task_... | null
  tool_run_id: trun_... | null
  result_id: res_... | null
```

`details` 是有界字段错误数组，不返回任意 debug JSON。内部异常类名、堆栈、SQL、绝对路径、密钥、Prompt、provider 原始响应和模型对象禁止出现。

### 13.5 主要公共错误码

```text
INVALID_REQUEST_BODY
VALIDATION_FAILED
TARGET_TASK_REQUIRED
TARGET_TASK_NOT_RECOVERABLE
TARGET_TASK_CONVERSATION_MISMATCH
IDEMPOTENCY_CONFLICT
RESOURCE_NOT_FOUND
TASK_NOT_RETRYABLE
EXPLANATION_NOT_RETRYABLE
ASSET_NOT_AVAILABLE
TOOL_UNAVAILABLE
DEPENDENCY_UNAVAILABLE
ASSET_PERSISTENCE_FAILED
RESULT_PERSISTENCE_FAILED
CHAT_ORCHESTRATION_FAILED
EXPLANATION_FAILED
UPSTREAM_TIMEOUT
TOOL_RUNTIME_TIMEOUT
EXPLANATION_TIMEOUT
INTERNAL_ERROR
```

Tool 特有输入错误继续使用第二、第三节已定义的安全错误码，并通过 validation details 返回。

## 14. Actor 所有权边界

### 14.1 当前匿名 ActorContext

MVP 请求进入 API 后，由本地上下文提供：

```text
ActorContext
  actor_id = 稳定本地匿名主体
  user_id = null
```

客户端不能通过请求体或任意 Header 自选 actor_id。API Adapter 构造 ActorContext 后，Application 才执行资源查询或写入。

### 14.2 必须检查所有权的资源

以下全部先经过 Actor 所有权检查：

- Conversation 创建后的列表和时间线；
- Message 提交目标 Conversation；
- target_task_id；
- Task 详情和 Tool 重试；
- ToolResult 查询和 Explanation 重试；
- Asset 元数据和内容下载。

Tool Catalog 与健康检查不读取用户业务资源，但仍只返回安全公共投影。

### 14.3 未来登录加入后的边界

未来认证中间件负责：

```text
认证凭据
→ 找到真实 user
→ 解析该 user 已安全认领的 actor_id 集合
→ 构造 ActorContext
```

Application 继续使用相同 actor 所有权检查。第四节 A 不设计用户表、密码、JWT、OAuth、角色、多租户或 Actor 认领流程。

为防资源枚举，业务资源不存在和不属于当前 Actor 的公共响应统一为 404；内部结构化日志可以区分真实 NOT_FOUND 与 OWNERSHIP_DENIED。

## 15. API 版本与 Tool Schema 版本

继续只使用：

```text
公共 API 路径版本：/api/v1
Tool 契约版本：schema_version
Tool 集成版本：tool_version
```

不引入 `model_version`。

规则：

1. `/api/v1` 约束公共路径、公共资源字段和错误包络。
2. `schema_version` 约束某个 Tool 的输入、ToolResult.data、warnings 和 provenance 结构。
3. `tool_version` 表示平台如何集成、校验、执行和组装该 Tool。
4. API v1 与 Tool schema_version 独立变化。
5. 新增可选字段、可忽略 warning 或新的 Tool Catalog 条目通常不需要 `/api/v2`。
6. 删除或重命名必填字段、改变字段含义、改变路径或破坏分页/错误语义时需要新公共 API 版本。
7. Tool 特有 data 的破坏性变化优先提升该 Tool 的 schema_version，不自动提升公共 API 版本。
8. 客户端不得依据 model bundle、权重文件名或内部 Runtime 版本选择模型。

## 16. 未来登录与 SSE

### 16.1 登录

当前契约已经把 ActorContext 放在 API 与 Application 边界：

- 当前从本地匿名上下文构造；
- 未来从认证中间件构造；
- 资源路径、ID 和响应结构不需要改变；
- 所有权仍由 actor_id 锚定；
- 本节不创建任何认证路径。

### 16.2 SSE

当前不创建：

```text
/api/v1/tasks/{task_id}/events
/api/v1/sse
事件类型
sequence
Last-Event-ID
重放缓冲
```

未来边界只描述为：

```text
TaskProgressReporter
→ SSE Adapter
→ 前端临时进度
```

最终状态继续通过：

```text
GET /api/v1/tasks/{task_id}
GET /api/v1/conversations/{conversation_id}/timeline
```

断线后前端重新读取持久化事实。当前不承诺进度事件可靠投递、补发、顺序或重放。

## 17. API 操作与持久化事实映射

| API 操作 | 读取/写入的正式事实 | 不允许使用的临时事实 |
|---|---|---|
| 创建 Conversation | Actor、Conversation | 前端临时会话对象 |
| 提交 NEW_TASK 消息 | UserMessage、Task、IdempotencyRecord、LLMCall；按结果产生 AssistantMessage 或 Revision/Tool 链路 | LLM 原始结构化输出直接响应 |
| 补充 NEEDS_INPUT | UserMessage、TaskInputRevision、IdempotencyRecord、可选 LLMCall/追问 Message | 覆盖旧 Message/Revision |
| 查询时间线 | Message、Task/ToolRun、ToolResult、ResultAssetLink/Asset、Explanation | 新建 TimelineItem 表或复制 ToolResult 为 Message |
| Tool 重试 | 新 ToolRun、幂等记录，后续新 Asset/Result；旧事实保留 | 重开或覆盖旧 ToolRun |
| Explanation 重试 | 新 Explanation、LLMCall、幂等记录 | 重新执行 Tool |
| 查询 ToolResult | 已持久化 ToolResult + Asset 公共投影 | Runtime 内存结果 |
| 下载 Asset | Asset AVAILABLE + StorageService 对象 | 客户端传入 object_key |
| Tool Catalog | Tool Registry 只读投影 | 数据库动态 Tool 表 |
| 健康检查 | 安全进程/依赖状态 | 模型权重细节或内部协议 |

所有外部 LLM、Tool Runtime 和 MinIO 网络操作发生在数据库事务之外。API 只在对应事实达到可返回状态后组装成功响应。

三个写入执行 POST 均在当前请求内同步等待稳定业务状态。客户端断开不自动转成取消；结果不确定时由相同 `Idempotency-Key` 重放读取原资源当前事实。

## 18. 验收场景

1. 新建 Conversation 返回 201 和稳定 conversation_id。
2. 向该 Conversation 提交知识问答，一次 CHAT_ORCHESTRATION 直接形成 AssistantMessage，Task SUCCEEDED，不创建 ToolRun/Asset/ToolResult。
3. 提交完整合法 Tool 请求，返回 Task、selected ToolRun/Result、结构化结果和 AVAILABLE Asset 引用。
4. Tool 意图明确但缺参数，返回 HTTP 200 + Task NEEDS_INPUT + 正式 missing_fields/追问，不创建 ToolRun。
5. 使用 `SUPPLEMENT_TASK + target_task_id` 补充成功，追加 Message/Revision 并执行 Tool。
6. 对话中存在多个 NEEDS_INPUT Task 时，不带明确 target_task_id 的补充被拒绝，系统不猜测。
7. 输入完整但材料、精度、单位、范围或 requested outputs 非法，返回 422，Task FAILED，不创建 ToolRun。
8. 同 Actor、同 operation、同 Idempotency-Key、同请求重复提交，返回原资源，不重复创建 Message、Task、Revision、ToolRun 或 Explanation。
9. 同 key 不同请求摘要返回 409，原资源不变。
10. 同时请求图片和性能，性能失败但图片可用时，HTTP 200，Task/ToolResult PARTIALLY_SUCCEEDED，时间线显示图片和性能错误。
11. 只请求性能但性能失败时，Task FAILED；中间 SEM 不被展示为已完成请求输出。
12. ToolResult 已成功但 Explanation 失败时，HTTP 200，Task PARTIALLY_SUCCEEDED，结构化结果和 Asset 继续可用。
13. 显式 Tool 重试创建新 ToolRun，旧 ToolRun/Asset/Result 保留且不混合。
14. 显式 Explanation 重试创建新 Explanation/LLMCall，不创建 ToolRun、不调用 Runtime。
15. 已成功 Task 请求“重新生成”时，前端通过消息入口创建新 Task，Tool 重试端点返回 TASK_NOT_RETRYABLE。
16. Task 状态、Tool 重试或 Explanation 重试后，重新查询时间线，原 Tool Task 的 anchor_at 和分页位置不变。
17. ToolRun 历史在时间线默认折叠，Task 详情可以查看全部尝试摘要。
18. Asset 非 AVAILABLE 时元数据没有 download_url，内容端点返回 409。
19. 当前 Actor 不能读取其他 Actor 的 Conversation、Task、Result 或 Asset；公共响应不泄漏资源是否存在。
20. Asset 内容响应具有正确 MIME、受控文件名和 inline/attachment 策略。
21. API JSON 和响应头不暴露 object_key、bucket、密钥、宿主机路径、权重路径、Prompt 或异常堆栈。
22. 新增第二个不同类型 Tool 时，继续使用同一 Message、Task、ToolRun、ToolResult、Asset、Explanation 和时间线 API；Tool 特有 data 由新 schema_version 约束。
23. `/health/live` 不运行模型；`/health/ready` 只返回安全依赖摘要，不暴露本地 Runtime 协议。
24. SSE 尚未实现时，Task 和时间线轮询仍能获得最终状态；断线重连不依赖事件重放。
25. API path version、tool_version 和 schema_version 能独立解释，响应中不存在 model_version。
26. 三个写入执行 POST 正常等待稳定业务状态后返回，不返回 `202`，不把 `PENDING/RUNNING` 作为正常最终响应。
27. Chat Orchestration 超时返回规范化 Task FAILED；Tool Runtime 超时返回 ToolRun/Task FAILED；均不返回未持久化内存结果。
28. ToolResult 已成功而 Explanation 超时时，HTTP 200、Task PARTIALLY_SUCCEEDED，并返回结构化结果与解释超时错误。
29. 客户端断开后使用同一 Idempotency-Key 重放，只读取原资源当前事实，不重复创建 Message、ToolRun 或 Explanation。
30. 未提供 Conversation 标题时首条消息后确定性生成默认标题，不调用标题专用 LLM；空白时回退“新对话”。
31. 图片默认 inline、显式下载使用 attachment；正式公网部署默认限制 `/health/ready` 为运维网络或已授权访问。

## 19. 阶段 1A 待验证事项

以下是未来实施前/中的非阻塞验证门槛，不是本轮实施计划：

1. 不透明 ID、UTC 时间、枚举和错误码的具体序列化一致性。
2. TimelineItem 判别联合在 OpenAPI/客户端生成中的可读性和向后兼容策略。
3. `submission_mode`、target_task_id 与 IdempotencyRecord.operation 的规范摘要一致性。
4. `Idempotency-Key` 长度、字符集、保留期和并发唯一冲突恢复。
5. 时间线 `(anchor_at, type_rank, item_id)` 游标编码、签名、升降序和最大 page size。
6. Task.updated_at、ToolRun 重试和 Explanation 重试不会改变既有 cursor 分页位置。
7. Tool Task 中 input thread、历史 ToolRun 和 ToolResult.data 的响应大小上限。
8. Task 详情完整 ToolRun 摘要在多次重试后的查询性能。
9. `result_id`、Asset 和 Task 的 actor 所有权过滤不会产生越权或资源枚举。
10. 受控 Asset 内容端点使用流式读取而非一次性加载大文件，并正确处理 MIME、文件名、HEAD/GET 和客户端断开。
11. 当前 PNG 文件大小下应用转发的吞吐足够；只有真实瓶颈出现时才切换短期签名 URL。
12. 非 AVAILABLE、对象缺失和 checksum 不一致时的 HTTP/Asset 状态映射。
13. CHAT_ORCHESTRATION 三类内部联合到公共响应的契约测试。
14. NEEDS_INPUT、硬校验失败、Tool 业务失败、Runtime 不可用、Asset 失败、Result 失败和 Explanation 失败的 HTTP/业务状态契约测试。
15. Tool Catalog 的 input_fields/output_summary 保持受控，新增 Tool 不需要修改公共核心 Schema。
16. `/health/ready` 的 core/degraded 分类及超时不会触发模型推理或长时间阻塞。
17. 公共 provenance 过滤不会泄漏 bundle、权重、路径、Runtime 地址或内部配置。
18. 未来认证中间件替换匿名 ActorContext 后，现有资源 API 不需要改变。
19. 三个同步写入 POST 在成功、业务失败和超时路径上都只返回稳定状态，且不存在 `202`/BackgroundTask/Worker 隐式继续执行。
20. Chat Orchestration、Tool Runtime 和 Explanation 的超时异常能规范化为稳定错误码；具体 Runtime 秒数留阶段 1B 实测。
21. 客户端断开时 ASGI 取消传播、关键持久化是否需要 shield、原请求可能继续执行的行为和资源清理边界。
22. 同一 Idempotency-Key 在原请求仍执行、已完成或连接断开三种时序下只绑定一个 Message/ToolRun/Explanation，并正确返回当前事实。
23. 默认 Conversation 标题的截断长度、Unicode/空白清理、并发首消息更新和固定回退值。
24. `/health/ready` 在本地直接访问与正式部署限制访问之间保持相同安全响应内容。

## 20. 已确认设计决定

### 20.1 Conversation 标题

已确认：调用者可提供可选标题。未提供时，在首条 UserMessage 成功保存后，使用确定性首消息截断生成默认标题；不调用标题专用 LLM，不影响消息提交成功。具体截断长度和字符规范留阶段 1A；空白或无法形成标题时使用固定默认值，例如“新对话”。

### 20.2 Asset 打开方式

已确认：图片默认使用 `Content-Disposition: inline`；显式下载时使用 `attachment`。未来新增其他真实资产类型时，再按 MIME/资产类型定义默认策略。

### 20.3 `/health/ready` 可见范围

已确认：本地 MVP 可以直接访问安全摘要；正式公网部署时默认限制为运维网络或已授权访问。第四节 A 只固定安全响应内容；认证、反向代理和网络访问限制留给部署/登录设计。

上述决定不改变 `/api/v1` 核心资源、幂等、时间线稳定锚点、所有权检查或第四节 A/B 分界。

## 21. 一致性与结束边界

本文继续采用：

```text
一次 CHAT_ORCHESTRATION
Message / ToolResult / Asset / NaturalLanguageExplanation 分层事实
Task 稳定时间线锚点
Task → 多个不可变 ToolRun
Task selected_tool_run_id / selected_result_id
TaskInputRevision.source_message_ids
ResultAssetLink
Asset PENDING / AVAILABLE / FAILED / ORPHANED
ActorContext 所有权边界
IdempotencyRecord
/api/v1
tool_version
schema_version
```

本文没有引入：

```text
model_version
TaskInputRevisionMessage
ToolResult/Explanation 的 AssistantMessage 副本
动态 Tool 管理 API
模型选择 API
真实 SEM 上传 API
SSE/WebSocket API
认证/角色 API
Worker/Queue API
本地 Runtime 公共路径或内部通信协议
```

第四节 A 完成后仍处于阶段 0。本轮已由独立第四节 B 设计本地 ZTA35G Tool Runtime 通信边界；本文件不重复或覆盖其内部协议。本轮不进入阶段 1，不创建实施计划，也不执行 git commit。
