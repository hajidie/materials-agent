# Tool Registry 与自然语言单 Tool Router 设计

日期：2026-09-01

状态：已完成对话评审，等待书面规范复核

## 1. 背景

Backend 已有 `StaticToolRegistry`、`ToolCatalogService` 和 `/api/v1/tools` 查询接口，但当前注册、LLM Structured Output、ZTA35G 参数候选、输入规范化、LLMCall 校验和应用启动接线仍围绕唯一的 `zta35g_sem_virtual_lab`。现有 Registry 更接近代码内固定映射，还不是能够支撑新增材料模型 Tool 的稳定扩展边界。

本设计把现有固定映射演进为受代码审查控制的开发者注册制，并泛化完整自然语言链路。生产环境在本阶段仍只注册 ZTA35G Tool；第二个异构 Tool 只存在于测试装配，用来证明扩展不再要求修改 Router、Registry 或执行核心。

## 2. 目标与非目标

### 2.1 目标

- 使用显式组合根注册 ToolDefinition，不扫描目录、不动态加载代码。
- 让 LLM 基于受控 `RoutingCatalogSnapshot` 提出 Tool 候选，但不授予执行权限。
- 让 `Registry.resolve()`、Tool 自有 `normalize()` 和 `Registry.authorize()` 分别负责解析、输入语义和授权。
- 支持唯一候选、参数缺失和多候选歧义，并保证 Task 一旦绑定 Tool 就不可切换。
- 用 `status` 表达生命周期，用 `execution_policy` 表达执行授权。
- 保存最小版本和执行契约身份，支持审计但不引入兼容矩阵。
- 保持每个模型 Tool 使用独立 Runtime；Backend 不吸收模型依赖。
- 为未来 Planner 保留统一候选数据边界，但当前继续采用单 Tool Router。

### 2.2 非目标

- 不实现动态插件上传、目录扫描、Python entry point 或运行时安装卸载。
- 不实现管理后台、运行时 Tool 开关或 DELETE Tool API。
- 不实现文件上传、训练数据接入、EBSD Tool、ML Training Tool、Planner、多 Agent 或多步计划执行。
- 不实现 SemVer 解析、兼容矩阵、自动 Schema 迁移或同一 Tool 多版本并行加载。
- 不改变真实 ZTA35G Runtime 的 Python 3.8 隔离、固定推理参数或单执行并发合同。

输入资产上传和 EBSD 接入是后续独立子项目。推荐顺序是：本注册与路由底座、用户输入资产生命周期、EBSD 性能预测 Tool。

## 3. 核心概念

### 3.1 ToolDefinition

每个 Tool 模块导出一个完整且自包含的 ToolDefinition。它至少提供：

- `tool_id`：稳定、不透明且全局唯一的标识；
- `version`：简单非空字符串，本阶段使用类似 `"1"` 的值；
- `status` 与 `execution_policy`；
- 安全展示信息和仅用于路由的简短能力描述；
- 影响执行语义的 `input_schema`；
- LLM 候选输入 Schema；
- 输入规范化器，能够返回 `Ready` 或受控 `NeedsInput`；
- Tool 执行器和独立 Runtime 健康检查适配器；
- 支持的输出类型、资产类型和限制说明。

展示名、description、UI 文案、Runtime URL、Token、内部路径和执行对象不属于执行 Schema，也不能进入 Schema Hash。

normalizer 使用统一接口 `normalize(candidate_input, prior_normalized_input=None)`。返回值只能是：

- `Ready(normalized_input, requested_outputs)`；
- `NeedsInput(normalized_input, missing_fields, ambiguous_fields, follow_up_suggestion)`。

返回对象及其中 JSON 必须经过受控类型、键集合和大小校验。预期的缺参或歧义必须通过 `NeedsInput` 表达，不能依赖异常控制正常流程。

### 3.2 ToolRegistry

ToolRegistry 是进程内不可变注册表，由应用组合根显式构建。它提供四类能力：

1. 启动校验：检查 ID、状态策略、Schema、执行边界和安全元数据；
2. `routing_snapshot()`：只投影允许新任务路由的安全信息；
3. `resolve()`：确认候选属于本次 Snapshot 且注册项仍存在；
4. `authorize()`：根据当前策略、操作类型和持久化 Task Binding 决定是否允许补参、执行或重试。

Registry 不保存数据库状态，不负责 LLM 推理，不解析 Tool 参数，也不执行模型。

### 3.3 显式组合

生产组合根显式列出 ZTA35G ToolDefinition。新增 Tool 时需要新增 Tool 模块、独立 Runtime 配置和一项显式注册；退役通过修改注册状态和策略完成，不删除注册项或历史数据。

不使用装饰器副作用、包扫描或 YAML 业务清单。Runtime 地址、Token 和超时继续由环境配置提供，但 Tool 业务契约由代码和测试定义。

## 4. 生命周期与执行策略

`status` 只表达产品生命周期：

- `ACTIVE`：正常使用；
- `DEPRECATED`：逐步退出；
- `DISABLED`：停止使用。

`execution_policy` 只表达授权：

- `ANY_TASK`：允许新任务、补参、首次执行和显式重试；
- `EXISTING_TASK_ONLY`：禁止新路由，只允许已绑定该 Tool 的 Task 补参、执行或显式重试；
- `NONE`：拒绝所有新的 ToolRun，包括重试。

MVP 只允许以下组合：

| status | execution_policy | 新路由 | 已绑定 Task | Readiness |
|---|---|---|---|---|
| `ACTIVE` | `ANY_TASK` | 允许 | 允许 | 检查 |
| `DEPRECATED` | `EXISTING_TASK_ONLY` | 排除 | Schema 未漂移时允许延续 | 检查 |
| `DISABLED` | `NONE` | 排除 | 补参、执行和重试全部拒绝 | 不检查，返回 `NOT_APPLICABLE` |

Registry 启动时拒绝其他组合，例如 `ACTIVE + NONE`、`DISABLED + ANY_TASK`。未来如果需要更细策略，应显式扩展合法矩阵，不能通过松散字符串绕过门禁。

状态通过代码变更和 Backend 重启生效。没有运行时开关、物理删除或动态卸载。

## 5. Schema Hash 与契约身份

`schema_hash` 只覆盖影响执行语义的 `input_schema`。计算过程固定为：

1. 只选择执行字段、类型、是否必填、单位、范围、精度、枚举和执行相关约束；
2. 生成键排序的 Canonical JSON；
3. 使用 UTF-8 和紧凑编码；
4. 拒绝 NaN、Infinity、非文本键和非标准 JSON 值；
5. 计算 SHA-256，结果为 64 位小写十六进制文本。

展示信息、description、UI 布局和其他非执行字段不得影响 Hash。Hash 只用于判断两个输入契约是否完全相同，不代表向前或向后兼容。

Registry 从 ToolDefinition 的 `input_schema` 计算 Hash，避免开发者手写值与 Schema 漂移。

## 6. Task Binding 与状态

Task 增加不可变 Tool Binding，物理字段为：

- `tool_id`；
- `bound_tool_version`：首次绑定时的 `version`；
- `bound_schema_hash`：首次绑定时的 `schema_hash`。

三个字段必须同时为空或同时非空。多候选歧义阶段 Binding 为空，受控候选引用保存在对应 TaskInputRevision 的 `candidate_tool_refs` JSON 数组中；每个引用只含 `tool_id`、`version` 和 `schema_hash`。Binding 一旦写入，不允许在同一 Task 生命周期内切换 Tool。

Task 增加 `READY` 状态。路由和规范化结果如下：

| 结果 | Binding | Task 状态 |
|---|---|---|
| 多个有效 Tool 候选 | 不绑定，保留受控候选引用 | `NEEDS_INPUT` |
| 唯一候选，缺少必要参数或参数歧义 | 绑定唯一 Tool | `NEEDS_INPUT` |
| 唯一候选，参数完整 | 绑定唯一 Tool | `READY` |

Binding 只能发生在 `Registry.resolve()` 成功，并且 `ToolDefinition.normalize()` 返回受控 `Ready` 或合法 `NeedsInput` 之后。LLM Candidate 阶段绝不冻结 Binding。resolve 失败、normalizer 抛出异常或返回非法结果时都不绑定。

写入 Binding 使用短事务内的比较更新或 Task 行锁。两个并发请求试图绑定不同 Tool 时，首个提交者成功；另一个收到冲突并重新读取，不能覆盖已有 Binding。

### 6.1 Task 与 ToolRun 状态职责

Task 和 ToolRun 是两层不同状态机。Task 表达用户科研任务从创建、补参到最终结束的业务阶段；ToolRun 只表达某一次模型执行尝试。Task 可以在没有 ToolRun 时进入 `NEEDS_INPUT` 或 `READY`，同一 Task 也可以因显式重试拥有多个 ToolRun。

Task 的业务阶段职责至少包括：

- `CREATED`：Task 已持久化，但路由、规范化或 Binding 尚未形成最终结论；
- `NEEDS_INPUT`：需要用户澄清 Tool 或补充参数，不能创建 ToolRun；
- `READY`：唯一 Tool 已绑定且输入完整，可以进入授权和执行；
- `RUNNING`：Task 当前选中的 ToolRun 正在执行；
- `COMPLETED`：Task 已产生终态结果，结果事实继续由 ToolResult、Asset 和现有完成明细表达；
- `FAILED`：Task 因 Agent 内部错误、授权后的执行失败或其他受控失败终止。

`CREATED` 和 `COMPLETED` 是本设计使用的业务阶段名称，不要求本次把现有持久化枚举机械改名：`CREATED` 对应现有执行前 `PENDING` 职责，`COMPLETED` 对应现有 `SUCCEEDED` 或 `PARTIALLY_SUCCEEDED` 终态职责。`READY` 是本设计新增的持久化状态，现有 `RUNNING`、`NEEDS_INPUT` 和 `FAILED` 继续保留。该映射避免仅为术语一致而扩大数据库迁移范围。

ToolRun 的持久化状态职责是：

- `PENDING`：已通过授权并保存执行快照，但尚未开始调用 Runtime；
- `RUNNING`：本次尝试已经开始，开始时间已持久化，Runtime 调用将在数据库事务外执行；
- `SUCCEEDED`：本次请求的全部输出成功；
- `PARTIALLY_SUCCEEDED`：只有部分请求输出成功，完成和失败集合必须完整且互斥；
- `FAILED`：本次尝试没有成功输出，保存受控错误与耗时。

ToolRun 不使用 `NEEDS_INPUT` 或 `READY`；这两个状态只属于 Task 的执行前业务阶段。

## 7. 首次自然语言路由

固定主链路是：

```text
User Request
    -> RoutingCatalogSnapshot
    -> LLM Structured Output / ToolCandidateSet
    -> Registry.resolve()
    -> ToolDefinition.normalize()
    -> Task Binding
    -> Registry.authorize()
    -> ToolRun Snapshot
    -> Tool Executor
    -> independent Runtime
```

`RoutingCatalogSnapshot` 只包含 `ACTIVE + ANY_TASK` Tool 的安全路由描述、输入字段摘要和输出能力。Runtime 地址、Token、完整 Prompt、内部路径和执行对象不进入 Snapshot。

首次路由 Structured Output 只有两种形状：

```json
{
  "route": "KNOWLEDGE_ANSWER",
  "answer_text": "..."
}
```

或：

```json
{
  "route": "TOOL_CANDIDATES",
  "candidates": [
    {
      "tool_id": "zta35g_sem_virtual_lab",
      "candidate_input": {}
    }
  ]
}
```

`TOOL_CANDIDATES` 包含 1–5 个不重复候选，整个解码对象不超过 4096 bytes。`candidate_input` 是受控 JSON object，请求输出也由各 Tool 的候选 Schema 定义在该对象中。Structured Output 不直接声明 READY、NEEDS_INPUT 或执行权限。

LLM 只提出该受控 ToolCandidateSet。应用层必须用生成该调用时的同一 Snapshot 验证候选 ID：

- 没有 Tool 意图时走知识回答；
- 多个有效候选进入未绑定的 `NEEDS_INPUT`；
- 唯一候选交给对应 Tool normalizer；
- Snapshot 外 ID、非法候选结构或 Structured Output Schema 不匹配属于 Agent 内部错误。

未来 Planner 可以产生相同的 ToolCandidateSet，但本阶段不增加 Planner 接口、计划实体或多步执行组件。

## 8. 已绑定 Tool 的后续补参

唯一候选但缺参时，Task 已绑定 Tool 并进入 `NEEDS_INPUT`。用户后续仍可发送自然语言补充，但该路径不得重新运行 Catalog Router，也不得重新选择 Tool。

补参链路是：

```text
User Supplement
    -> load immutable Task Binding
    -> Registry.resolve(bound tool)
    -> Registry.authorize(SUPPLEMENT)
    -> fixed Tool input extractor
    -> ToolDefinition.normalize(previous input + candidate delta)
    -> new TaskInputRevision
    -> READY or NEEDS_INPUT
```

固定 Tool 参数提取器只接收已绑定 Tool 的候选 Schema。其 Structured Output 只有 `candidate_input_delta`，不包含 `tool_id`、`route` 或其他候选列表，因此不能切换 Tool。

normalizer 使用上一 TaskInputRevision 的规范化输入和本次 delta：新字段覆盖同名旧值，未提供字段继承上一 Revision。它返回完整的新规范化输入、缺失字段和歧义字段。参数完整时 Task 进入 `READY`，否则继续 `NEEDS_INPUT`。

固定提取使用独立的 LLMCall purpose `TOOL_INPUT_EXTRACTION`。如果 Binding 缺失、当前策略拒绝或 Schema 已漂移，不调用提取 LLM。提取结果非法时归类为 Agent 内部错误，不写入新 Revision。

## 9. 授权、执行与 ToolRun 快照

首次执行、既有 Task 执行、补参和显式重试都经过 Registry 当前策略授权。历史策略快照只能解释过去，不能替代当前授权。

授权通过后，在短事务中创建 PENDING ToolRun。ToolRun 至少保存：

- `tool_id`；
- 执行时当前 `version`；
- 当前 `schema_hash`；
- 完整 `normalized_input_snapshot`；
- 实际发给 Runtime 的 `execution_input`，包括受控 Runtime 参数；
- `execution_policy_snapshot`；
- `task_input_revision_id`、attempt、seed 和现有结果追踪字段。

ToolRun 状态按以下顺序流转：

1. `authorize()` 通过后，在短事务中创建并提交 `PENDING` ToolRun；
2. 在下一段短事务中执行 `PENDING -> RUNNING`，保存 `started_at` 并提交；
3. 只有 RUNNING 已提交后，才在数据库事务外调用独立 Runtime；
4. Runtime 返回后，在新的短事务中把当前 ToolRun 更新为 `SUCCEEDED`、`PARTIALLY_SUCCEEDED` 或 `FAILED`，并保存完成时间、耗时和受控结果摘要；
5. Runtime 超时、不可用、协议错误或受控模型错误只会使当前 ToolRun 进入 `FAILED`，不会改写该 Task 的任何旧 ToolRun。

Runtime 失败后不自动重试。显式重试必须重新执行当前策略授权，并创建新的 ToolRun、attempt 和 seed；旧 ToolRun 不覆盖。

## 10. 版本变化与契约漂移

Task Binding 保存首次绑定时的版本和 Schema Hash，但每个 ToolRun 保存实际执行时的当前版本。

- 同一 `tool_id` 的 `version` 从 `"1"` 变为 `"2"`，而 `schema_hash` 不变时，已有 Task 可以继续补参、执行或重试；新 ToolRun 记录 `version="2"`，原 Binding 不改写。
- `schema_hash` 变化时，已有 Task 不得自动迁移，也不得调用固定提取器或 Runtime；系统要求用户创建新 Task。

这是一条相等性门禁，不是兼容性推断。系统不实现自动迁移或版本兼容矩阵。

## 11. LLMCall 审计

LLMCall 不保存完整 Prompt 和 Provider 原始响应。首次路由至少保留：

- provider 和 model；
- `catalog_snapshot_refs`：本次安全 Snapshot 的 `tool_id`、`version`、`schema_hash` 引用；
- `catalog_hash`：完整安全 RoutingCatalogSnapshot 的 Canonical JSON SHA-256；
- `structured_output_summary`：Tool 路由和固定提取保存受控解码对象；知识回答只保存 answer length 和 digest；
- 现有 `prompt_digest`；
- Provider request ID、usage、duration 和安全错误等现有字段。

固定 Tool 补参使用 `TOOL_INPUT_EXTRACTION` purpose，在 `tool_context_ref` 中保留绑定 Tool 的 `tool_id`、当前 `version` 和 `schema_hash`，并在 `structured_output_summary` 中保存受控候选增量，不保存原始响应。

## 12. Catalog API

`GET /api/v1/tools` 和单 Tool 查询继续由 Registry 投影，至少返回：

- `tool_id`、display name 和安全 description；
- `version`、`schema_hash`；
- `status`、`execution_policy`；
- availability；
- 输入字段摘要、输出能力、资产类型和限制。

`DISABLED + NONE` 注册项继续显示，但 availability 为 `NOT_APPLICABLE`，且不触发 Runtime readiness。Catalog 查询不授予执行权限。

## 13. 错误处理

### 13.1 启动失败

以下结构问题使 Backend 拒绝启动：

- 重复或不安全的 Tool ID；
- 非法状态与策略组合；
- 允许执行的注册项缺少 normalizer 或 executor；
- input_schema 无法 Canonical JSON 化或超出安全限制；
- Catalog 元数据包含 Secret、内部路径、非法类型或超限文本。

外部 Runtime 暂时不可达不是注册结构错误。Backend 可以启动，Catalog 标记 UNAVAILABLE；实际调用按受控依赖失败处理。

### 13.2 请求期结果

- 多 Tool 候选、用户缺参和参数歧义是 `NEEDS_INPUT`，不是错误。
- LLM Structured Output Schema 不匹配、Snapshot 外 Tool ID 或非法候选结构，对产品层归类为 `AGENT_INTERNAL_ERROR`；`LLM_SCHEMA_MISMATCH` 可作为内部原因码。不得提示用户修改输入，不绑定 Task，不创建 ToolRun，也不保存原始响应。
- authorize 拒绝返回受控 `TOOL_EXECUTION_NOT_ALLOWED`，不创建 ToolRun，历史事实不变。
- Runtime 超时、不可用、协议错误或受控 Runtime 错误使已创建 ToolRun 进入 FAILED，保存安全错误和耗时。

## 14. 事务、并发与安全

- Task Binding、状态比较、输入 Revision 和 PENDING ToolRun 使用短事务及现有乐观/行锁语义。
- LLM、Runtime、MinIO 和其他外部调用不进入数据库长事务。
- 请求幂等键继续避免重复 Task、Revision 或执行尝试。
- Registry 在进程生命周期内不可变，状态只在代码变更和重启后切换。
- 不记录 Tensor、图片 bytes、完整 Prompt、完整 Provider 响应、Secret、权重路径或内部绝对路径。
- Candidate、Schema、Catalog 和审计 JSON 都使用受控类型与大小上限。

## 15. 数据迁移要求

实现需要同步领域模型、数据库约束、Alembic 迁移、Repository 和 API 投影，目标字段明确为：

- Task：新增可空 `tool_id`、`bound_tool_version`、`bound_schema_hash`，约束三者同时为空或同时非空；Task 状态约束增加 `READY`；
- TaskInputRevision：新增非空 JSONB `candidate_tool_refs`，默认空数组并约束为数组；多候选时保存 2–5 个不重复受控引用；
- ToolRun：保留 `tool_version` 和实际 `execution_input`；以 `schema_hash` 取代现有 `schema_version` 语义；新增非空 JSONB `normalized_input_snapshot` 和非空 `execution_policy_snapshot`；
- LLMCall：新增可空 JSONB `catalog_snapshot_refs`、可空 `catalog_hash`、可空 JSONB `tool_context_ref`，purpose 约束增加 `TOOL_INPUT_EXTRACTION`；继续使用现有 `prompt_digest` 和受控 `structured_output_summary`；
- Catalog：availability 枚举增加 `NOT_APPLICABLE`。

现有 ZTA35G ToolRun 的 `schema_hash` 使用当前 ZTA35G input_schema 的确定性常量回填，`normalized_input_snapshot` 从其 `task_input_revision_id` 指向的 Revision 回填，`execution_policy_snapshot` 回填为 `ANY_TASK`。已有 Tool Task 的 Binding 从持久化 LLMCall、TaskInputRevision 和 ToolRun 事实按一致性门禁回填；发现冲突时迁移失败，不猜测或覆盖。迁移不得删除 ToolRun、ToolResult 或 Asset。实施计划可以拆分迁移步骤，但不能改变上述目标字段和回填规则。

## 16. 测试与验收

### 16.1 领域与单元测试

- 重复 ID、不安全 ID、非法状态策略组合和不完整可执行注册项启动失败；
- Schema canonicalization 对键顺序稳定、拒绝非标准 JSON，且展示字段变化不改变 Hash；
- `resolve()` 只接受本次 Snapshot 中的候选；
- `authorize()` 覆盖 ANY_TASK、EXISTING_TASK_ONLY、NONE 的补参、执行和重试矩阵；
- Task Binding 只能在 resolve 与受控 normalize 结果之后写入，并且不可切换 Tool；
- Ready、合法 NeedsInput、非法 normalizer 结果和 Agent 内部错误分类正确。
- Task 业务阶段与 ToolRun 执行状态分别验证，Task 的 NEEDS_INPUT/READY 不会误创建 ToolRun 状态；
- ToolRun 只允许 PENDING -> RUNNING -> 终态的合法转换，不能跳过 RUNNING 或从终态重新开始。

### 16.2 自然语言与补参测试

- 唯一候选且参数完整：绑定 Tool，Task 进入 READY，授权后创建 ToolRun；
- 唯一候选且缺参：绑定 Tool，Task 进入 NEEDS_INPUT；
- 用户补参：Router 调用次数为 0，不生成 RoutingCatalogSnapshot，不重新选择 Tool；
- 固定提取器只返回 `candidate_input_delta`，随后由已绑定 Tool normalizer 合并上一 Revision；
- 补参完整后进入 READY，仍缺参时保持 NEEDS_INPUT；
- 多个候选：Task.tool_id 为空，保存候选引用并等待澄清；澄清后才 resolve、normalize 和绑定；
- Snapshot 外 ID 或非法 Structured Output 归类 AGENT_INTERNAL_ERROR，且不绑定、不创建 ToolRun。

### 16.3 生命周期与契约迁移测试

- ACTIVE + ANY_TASK 参与新路由并允许所有正常入口；
- DEPRECATED + EXISTING_TASK_ONLY 不参与新路由，但允许 Schema 未漂移的已绑定 Task；
- DISABLED + NONE 拒绝补参、执行和重试，不创建 ToolRun，历史查询仍可用；
- version 更新且 schema_hash 不变时，已有 Task 可以继续，新 ToolRun 记录当前 version；
- schema_hash 变化时拒绝旧 Task 自动迁移，不调用提取 LLM 或 Runtime。

### 16.4 异构 Tool 测试

测试装配增加一个 ML Training 风格 ToolDefinition，例如字段：

```json
{
  "dataset": "dataset_fixture_1",
  "task_type": "regression",
  "split_ratio": 0.8,
  "target_column": "yield_strength",
  "shuffle": true
}
```

它的字段、候选 Schema 和输出类型与 ZTA35G 完全不同。`dataset` 只是测试用不透明 ID，不代表文件上传或训练数据接入。

测试必须证明增加该 Tool 只需要 ToolDefinition、测试执行适配器和测试注册项；Router、Registry、Task Binding 和执行核心不修改。该 Tool 不进入生产组合根。

该测试的唯一产品结论是 Registry 与 Single Tool Router 能承载不同能力契约。它不能用于声称生产已经支持模型训练、训练数据上传、EBSD、Planner 或多 Agent。

### 16.5 数据库、并发、安全与回归

- 迁移测试覆盖 READY、Binding 约束、ToolRun 新快照字段、历史回填和 LLMCall 审计；
- 并发绑定不同 Tool 时只有一个提交者成功；
- 策略快照不能绕过当前 Registry 授权；
- Catalog、LLMCall、公共响应和日志不泄露 Secret、端点、路径、完整 Prompt 或原始响应；
- Runtime 外调不占用数据库长事务；
- authorize 后先提交 PENDING、Runtime 前提交 RUNNING、Runtime 后提交终态，且失败尝试不覆盖旧 ToolRun；
- Backend 全量、迁移、契约和现有 ZTA35G 回归测试通过；
- 最终运行 `git diff --check`、精确范围检查和 `git status`。

真实 DeepSeek、真实 ZTA35G 模型加载、GPU 推理和浏览器 E2E 不作为本次离线完成门禁，除非实施任务另行明确要求；未运行时必须如实报告。

## 17. 完成定义

满足以下条件才可声明扩展底座完成：

1. 生产仍只注册 ZTA35G Tool，README 明确“生产单 Tool、代码架构支持显式扩展”。
2. 自然语言首次路由、固定 Tool 补参、不可变 Binding、策略授权和 ToolRun 快照合同均由测试覆盖。
3. ML Training 风格测试 Tool 无需修改核心即可通过完整链路。
4. Tool 退役只改变状态和策略，不物理删除注册项、ToolRun、ToolResult 或 Asset。
5. 没有动态插件、文件上传、EBSD、Planner、多 Agent 或生产部署能力的误导性声明。
6. 受影响的回归、迁移、契约和范围门禁通过，外部环境未验证项被单独标注。
