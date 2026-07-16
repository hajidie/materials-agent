# 材料智能体平台阶段 0 第一节：ZTA35G SEM Virtual Lab MVP 范围与总体架构

> 状态：已确认设计基线
>
> 修订日期：2026-07-15
>
> 适用范围：阶段 0 文档设计
>
> 事实依据：本轮重新检查当前仓库并完整读取第一节和第二节最新文档；继续采用此前对 `SEM/` 的静态只读事实。本轮未修改或运行 `SEM/`，未导入模型、未加载权重、未安装依赖，也未执行训练或推理。

## 1. 本次修订结论

MVP 只注册一个材料范围明确的复合 Tool：

```text
tool_id: zta35g_sem_virtual_lab
Tool: ZTA35GSEMVirtualLabTool
适用材料: ZTA35G 钛合金
```

平台把第三方材料 Tool 主要视为黑盒能力。公共边界只关注输入、输出、执行成败、平台链路和可定位的大环节，不抽象 Tool 内部模型阶段，不管理内部张量、特征或模型生命周期。

统一公共执行契约为：

```text
MaterialTool.execute(validated_input, request_context)
→ ToolExecutionOutput
```

`ZTA35GSEMVirtualLabTool` 或其内部执行 Adapter 自己完成：

```text
四维工艺参数
→ DDPM 生成一张 SEM 图像
→ 按 requested_outputs 决定是否继续
→ DenseNet121 提取图像特征
→ 图像特征与四维参数拼接
→ 两个 SVR
→ 返回图片、性能结果、错误和 diagnostics
```

Application 不分别调用 `generate_sem` 和 `predict_mechanical_properties`，也不在 DDPM 与 DenseNet/SVR 之间协调。图片资产是结果提交检查点，不是模型内部阶段之间的执行检查点：Tool 可以先完成完整计算并一起返回图片和性能；Application 必须先把必需图片保存为 `AVAILABLE`，然后才可提交引用该图片的成功 ToolResult。

公共运行关系简化为：

```text
Task
└─ 一个或多个 ToolRun
```

每一次实际 Tool 执行尝试创建新的 `tool_run_id`。平台不保留 InferenceRun、PredictorRawOutput 或 StageRun 公共实体。

正式版本字段只保留：

```text
tool_version
schema_version
```

模型文件只使用内部 `model_bundle_id`、权重文件 SHA-256 和兼容性验收记录进行轻量溯源及完整性检查，不建设模型发布、选择、灰度或回滚系统。

## 2. 已确认的第三方工具事实

### 2.1 文件与代码分类

```text
SEM/
├─ initial_para.csv
├─ train_images/
│  └─ 50 个 512×512 PNG 图像
└─ ZTA35G_lab/
   ├─ virtual_lab_sem.py
   ├─ Update_SVR_model.py
   ├─ ddpm_512_epoch_800.pth
   ├─ densenet121-a639ec97.pth
   └─ SVR_model/
      ├─ Final_Yield_Strength.pkl
      └─ Final_Elongation.pkl
```

| 类别 | 当前事实 | 阶段 0 结论 |
|---|---|---|
| 训练代码 | 未发现可确认的训练循环、优化器、损失计算或模型 `fit` 流程 | MVP 不建设训练能力或训练数据版本管理 |
| 模型结构 | 条件 DDPM U-Net、Torchvision DenseNet121、两个序列化 SVR | 只作为 Tool 内部事实，不进入平台公共抽象 |
| 推理代码 | `VirtualLab.generate_images`、`extract_features`、`predict_performance`、`run` | 当前是耦合的完整虚拟实验链路 |
| 模型迁移脚本 | `Update_SVR_model.py` 会加载并覆盖保存两个 Joblib 文件 | 阶段 0 不运行，不修改权重 |

### 2.2 权重加载风险

| 模型 | 权重 | 已知加载方式或事实 | 阶段 1B 风险 |
|---|---|---|---|
| 条件 DDPM U-Net | `ddpm_512_epoch_800.pth` | `torch.load`；优先 `ema`，其次 `model`；清理键前缀；`strict=False` | 必须记录 missing/unexpected keys 并判断功能影响 |
| DenseNet121 | `densenet121-a639ec97.pth` | 构造 `densenet121(weights=None)`；转换旧版 denselayer 键名 | PyTorch/Torchvision 兼容组合待验证 |
| 屈服强度 SVR | `Final_Yield_Strength.pkl` | `joblib.load` | 序列化环境涉及 scikit-learn 1.0.2 |
| 延伸率 SVR | `Final_Elongation.pkl` | `joblib.load` | 序列化环境涉及 scikit-learn 1.0.2 |

### 2.3 输入与内部流程事实

| 代码参数 | Tool 输入字段 | 业务名称 | 类型与精度 | 标准单位 | 允许范围 |
|---|---|---|---|---|---|
| `T1` | `solution_temperature` | 固溶温度 | 整数 | °C | 900–1100 |
| `t1` | `solution_time` | 固溶时间 | 数值，最多 1 位小数 | h | 1–5 |
| `T2` | `aging_temperature` | 时效温度 | 整数 | °C | 670–790 |
| `t2` | `aging_time` | 时效时间 | 数值，最多 1 位小数 | h | 1–5 |

正式内部流程为：

```text
四维工艺参数
→ DDPM 生成一张单通道 SEM 图像
→ DenseNet121 提取图像特征
→ 图像特征与四维工艺参数拼接
→ 屈服强度 SVR + 延伸率 SVR
→ 屈服强度（MPa）+ 延伸率（%）
```

性能预测必须携带四维工艺参数，只使用本次 Tool 内部生成的单张 SEM 图像。MVP 不支持用户上传真实 SEM 图像后直接预测性能，也不执行多图特征平均。

## 3. 阶段边界与 MVP 范围

### 3.1 当前阶段

当前仍处于阶段 0，只允许建立项目章程、架构设计、技术决策、路线图和开发规范。本轮只修订第一节和第二节文档。

阶段 0 不执行：

- 创建或修改业务代码、FastAPI 项目、数据库表或 Alembic migration；
- 创建 Docker Compose、Conda 环境或安装依赖；
- 实现 Tool、Adapter、模型 Runtime、LangChain 或前端；
- 运行模型、加载权重或修改、移动、删除 `SEM/`；
- 创建阶段 1 实施计划或进入阶段 1A/1B；
- 执行 git commit。

### 3.2 MVP 能力

平台长期目标是让用户通过自然语言提交材料任务，由系统判断是否调用材料 Tool，校验参数、接收或获得文件，并返回结构化结果、文件和自然语言解释；后续持续增加材料 Tool，并在 MVP 后增加流式传输、正式登录系统，最终部署为正式网站。当前阶段只为这些方向保留稳定边界，不提前实现对应基础设施。

MVP 支持三个请求模式：

1. 只请求 `sem_image`；
2. 只请求 `mechanical_properties`，但 Tool 内部仍生成 SEM，且平台最终保存该中间 SEM；
3. 同时请求两项，Tool 只生成一张 SEM 并复用它完成性能链路。

平台继续支持自然语言任务、确定性参数校验、结构化 ToolResult、文件资产、自然语言解释、对话和任务查询。MVP 使用 LangChain，不引入 LangGraph、通用工作流引擎、多智能体或动态 DAG。

### 3.3 MVP 非范围

MVP 不实现：

- 真实 SEM 上传后直接预测性能；
- EBSD、图像分割、裂纹检测或第二个材料 Tool；
- Tool 内部训练、模型结构抽象、张量生命周期或特征存储；
- 模型发布、动态版本选择、灰度、在线回滚或模型注册服务；
- Redis、Worker、后台队列、可靠事件总线、Event Store；
- SSE、WebSocket 或事件重放；
- OpenTelemetry、LangSmith、Metrics 和告警平台；
- 正式登录、角色矩阵、多租户、项目权限或计费；
- GPU 容器、GPU 集群、自动扩缩容或通用微服务架构；
- 生产级网站部署。

## 4. 总体架构原则

平台遵循：

```text
当前实现尽量简单
+ 少量稳定接口
+ 未来扩展时不大范围重构
```

具体原则：

1. 平台只理解 Tool 的业务输入输出和主要诊断，不理解 DDPM、DenseNet、SVR 内部对象。
2. Tool 自己决定内部执行顺序；Application 只调用一次公共 `execute`。
3. PostgreSQL 保存结构化业务事实，MinIO 保存文件本体。
4. 资产必须先达到 `AVAILABLE`，引用它的 ToolResult 才能作为成功结果提交。
5. Task、ToolRun、Asset、ToolResult 和 Explanation 是平台核心关系；不为未来可能性预建额外运行层级。
6. 结构化日志满足 MVP 排错；TaskProgressReporter 只为未来用户进度展示保留最小边界。
7. 本地独立 Tool Runtime 只解决旧 Python/模型依赖兼容，不代表平台改为微服务架构。

## 5. 模块化单体总体架构

```text
Vue 3 + Vite Frontend
        ↓
FastAPI API（未来公共路径 /api/v1）
        ├─ 构造 ActorContext
        └─ 建立 request_id / conversation_id / task_id
        ↓
Application Services
        ├─ ChatOrchestrationService + LangChain
        ├─ TaskService
        ├─ ToolExecutionService
        ├─ AssetService
        └─ ResultService
        ↓
Core Contracts
        ├─ MaterialTool
        ├─ Tool Registry
        ├─ Tool Catalog 只读投影
        ├─ StorageService
        ├─ Repository Ports
        └─ TaskProgressReporter
        ↓
Infrastructure / Integration Adapters
        ├─ PostgreSQL Repositories
        ├─ MinIO StorageService
        ├─ Structured Logging
        ├─ NoOp 或 StructuredLoggingTaskProgressReporter
        └─ Local ZTA35G Tool Client Adapter
                ↓ 127.0.0.1
           ZTA35G Tool Runtime
           （materialsagent-zta35g）
```

### 5.1 模块职责

- `ChatOrchestrationService`：让 LangChain 生成结构化意图和候选参数，决定知识回答、追问或允许的 Tool 用例。
- `TaskService`：创建 Task、维护当前状态、处理 `NEEDS_INPUT` 恢复和显式重试选择。
- `ToolExecutionService`：创建 ToolRun，调用一次 `MaterialTool.execute`，保存 diagnostics，协调资产和结果提交。
- `AssetService`：通过 StorageService 管理 `PENDING / AVAILABLE / FAILED / ORPHANED` 资产生命周期和跨存储一致性。
- `ResultService`：提交 ToolResult 和 NaturalLanguageExplanation，保证解释不覆盖结构化结果。
- `ZTA35GSEMVirtualLabTool`：执行输入复核、调用内部 Tool Client Adapter、把 Runtime 响应整理成 ToolExecutionOutput。
- `ZTA35G Tool Runtime`：在旧模型环境中加载并复用模型，执行完整黑盒链路；不访问平台数据库或对象存储。

底层服务不得反向依赖 ChatOrchestrationService。UnitNormalization 可以是 Application 内部小型确定性组件，不建设通用科学单位平台。

### 5.2 LangChain 与 Application 边界

LangChain 只负责：

- 判断是否需要 `zta35g_sem_virtual_lab`；
- 提取四维参数的原始数值和单位候选；
- 提议 `requested_outputs`；
- 识别缺失或歧义并生成追问；
- 基于已持久化结构化事实生成自然语言解释。

LangChain 不直接执行 Tool，不选择 Tool 或模型文件版本，不做最终单位换算，不控制 `sem_generation` 或 `mechanical_property_prediction`，也不得编造图像、性能、置信度或适用性数值。

Application 负责：

- 确定性单位转换、去重和完整硬校验；
- 通过 Tool Registry 解析启用的 Tool；
- 创建 Task 和每次实际执行的 ToolRun；
- 调用一次 `MaterialTool.execute`；
- 先提交必需图片资产，再提交引用资产的 ToolResult；
- 聚合 completed/failed outputs、ToolRun 和 Task 状态；
- 保存解释及其独立失败或重试事实。

### 5.3 Tool Registry 与 Tool Catalog

Tool Registry 是工具元数据和可执行 Tool 的唯一事实来源。MVP 使用代码级静态注册，不使用数据库动态注册或插件市场。

通用元数据至少允许表达：

```text
tool_id
tool_version
schema_version
enabled
availability
input_schema
output_schema
supported_outputs
supported_asset_types
execution_mode
requires_gpu
```

`enabled` 表示平台配置是否允许使用；`availability` 表示当前依赖、健康检查和兼容性验收是否通过。Tool Catalog 从 Registry 元数据生成只读投影，不单独持久化，必须明确 ZTA35G 限定、四维输入、两个 requested outputs 和不支持真实 SEM 上传。

## 6. Tool 公共契约与内部执行边界

### 6.1 `MaterialTool`

所有 Tool 实现统一最小契约：

```text
metadata
validate_input
execute
health_check
```

其中：

```text
MaterialTool.execute(validated_input, request_context)
→ ToolExecutionOutput
```

通用 MaterialTool 不假设一定存在机器学习模型、Predictor、GPU 或内部阶段。正常新增 Tool 不应要求修改 StorageService、TaskService、公共 ToolResult 或现有 Tool。

### 6.2 `request_context`

公共 request context 只携带 Tool 执行所需的平台上下文：

```text
request_id
conversation_id
task_id
tool_run_id
actor_context
requested_at
```

不包含 `trace_id` 或 `inference_run_id`，也不携带模型内部对象。`conversation_id` 和 `actor_id` 是会话/主体关联标识，不属于最小运行尝试标识集合。

### 6.3 `ToolExecutionOutput`

ToolExecutionOutput 至少允许表达：

```text
status
requested_outputs
completed_outputs
failed_outputs
data
images
warnings
diagnostics
provenance
error
```

`images` 携带可由 AssetService 编码和保存的图片内容或等价中立表示，以及图片角色和必要元数据。公共契约不得暴露 `torch.Tensor`、CUDA Tensor、DenseNet 特征、SVR 中间值或 Remote 临时张量 token。

diagnostics 是轻量通用结构，至少允许：

```text
step
status
started_at
completed_at
duration_ms
error_code
safe_error_message
```

ZTA35G Tool 常用 step 为：

```text
sem_generation
mechanical_property_prediction
```

diagnostics 可以作为 ToolRun JSON/JSONB、ToolExecutionOutput 字段或等价轻量结构保存，不建立独立 Stage 实体或强制数据库表。

### 6.4 三种执行模式

只请求 `sem_image`：

```text
Tool.execute
→ sem_generation
→ 返回一张 SEM 图片
→ mechanical_property_prediction 不执行
```

只请求 `mechanical_properties`：

```text
Tool.execute
→ sem_generation
→ DenseNet121 + 两个 SVR
→ 返回中间 SEM 图片和性能结果
```

同时请求两项：

```text
Tool.execute
→ 只生成一张 SEM
→ 复用同一图片完成性能链路
→ 一起返回图片和性能结果
```

Tool 对可捕获异常应尽可能返回已生成 SEM、已成功输出、失败输出、结构化错误和主要步骤 diagnostics。MVP 不保证进程崩溃、GPU 进程直接退出或宿主机崩溃时保留中间 SEM。

### 6.5 结果提交检查点

Application 收到 ToolExecutionOutput 后必须：

1. 找出该输出要求保存的全部 SEM 图片，包括只请求性能时的中间图片；
2. 通过 AssetService 保存图片；
3. 仅在 Asset 达到 `AVAILABLE` 后，允许 ToolResult 引用该 `asset_id` 并提交成功或部分成功结果；
4. 图片保存失败时，不向用户提交不可追溯的性能成功结果；
5. 保存 Tool 返回的 diagnostics 和安全错误，不能把资产失败改写成模型内部阶段失败。

资产检查点发生在 Tool 完整计算返回之后，因此不要求模型内部停在 DDPM 与 DenseNet/SVR 之间。

## 7. 输入、标准化与校验

### 7.1 用户输入

```json
{
  "process_parameters": {
    "solution_temperature": 1000,
    "solution_time": 3.0,
    "aging_temperature": 730,
    "aging_time": 3.0
  },
  "requested_outputs": [
    "sem_image",
    "mechanical_properties"
  ]
}
```

`requested_outputs` 是非空集合，只允许 `sem_image` 和 `mechanical_properties`。合法重复值由 Application 确定性去重；空集合或未知值返回 `INVALID_REQUESTED_OUTPUT`。

ZTA35G 是固定适用材料。明确请求其他材料时返回 `UNSUPPORTED_MATERIAL`；材料意图不明确时进入追问，不自动泛化 Tool。

### 7.2 确定性校验

- 四个工艺参数全部必填，LLM 或代码不得静默补全；
- 温度标准化值必须是整数；
- 时间最多 1 位小数；
- 参数必须位于第 2.3 节范围；
- 不得自动截断、四舍五入、交换字段或用默认值替换缺失值；
- 超出范围必须拒绝，不采用“警告后继续”；
- Tool 的 `validate_input` 对 Application 校验结果进行边界复核。

主要错误码包括：

```text
MISSING_PROCESS_PARAMETER
INVALID_PROCESS_PARAMETER_PRECISION
INVALID_PROCESS_PARAMETER_UNIT
PROCESS_PARAMETERS_OUT_OF_RANGE
INVALID_REQUESTED_OUTPUT
UNSUPPORTED_MATERIAL
```

### 7.3 单位标准化

Application 使用最小确定性规则：温度使用 °C；时间接受明确的 h 和 min，并按 `60 min = 1 h` 转换。保存原始值/单位、标准化值/单位、转换规则和转换错误。LLM 只提出候选，不做最终换算，也不猜测缺失单位。

## 8. Actor、标识、关系、状态与重试

### 8.1 `ActorContext`

```text
ActorContext
  actor_id
  user_id
```

MVP 使用稳定本地匿名主体：

```text
actor_id = 稳定本地匿名主体
user_id = null
```

Conversation、Task、Asset 和 Result 的持久化边界允许关联 `actor_id`。当前不保留 `project_id`、`tenant_id`、角色矩阵、多租户路由、计费或复杂权限模型。未来认证层只需构造真实 ActorContext 并增加访问控制检查。

### 8.2 最小运行标识集合

MVP 主要执行关联只使用：

```text
request_id
task_id
tool_run_id
```

- `request_id`：一次 HTTP 请求；补充、显式重试和解释重试均新建。
- `task_id`：稳定业务任务；`NEEDS_INPUT` 恢复和显式整体 Tool 重试时保留。
- `tool_run_id`：一次实际 Tool 执行尝试；每次实际执行或显式重试均新建。

`conversation_id`、`actor_id`、`asset_id`、`result_id` 和 `explanation_id` 分别是会话、主体和资源标识，不增加新的运行层级。MVP 不强制独立 `trace_id`。

### 8.3 核心关系

```text
Conversation(actor_id)
└─ Task(task_id, actor_id)
   ├─ ToolRun A(tool_run_id, diagnostics)
   │  ├─ Asset(s)
   │  └─ ToolResult
   ├─ ToolRun B(tool_run_id, diagnostics)  ← 显式重试
   │  ├─ Asset(s)
   │  └─ ToolResult
   ├─ selected_tool_run_id
   └─ selected_result_id
      └─ NaturalLanguageExplanation(s)
```

Asset、ToolResult 和 diagnostics 直接关联实际产生它们的 `tool_run_id`。Task 逻辑上只需明确 `selected_tool_run_id` 和 `selected_result_id`，不能通过“最后创建”隐式选择。

### 8.4 状态集合

Task：

```text
PENDING
RUNNING
NEEDS_INPUT
SUCCEEDED
PARTIALLY_SUCCEEDED
FAILED
```

ToolRun：

```text
PENDING
RUNNING
SUCCEEDED
PARTIALLY_SUCCEEDED
FAILED
```

MVP 不保留 `CANCELLED`、`TIMED_OUT`，等真实取消和超时控制设计时再增加。实体至少保存：

```text
current_status
created_at
started_at
completed_at
duration_ms
error_code
safe_error_message
```

MVP 不强制每次状态变化建立独立历史表。详细执行过程由结构化日志和 ToolRun diagnostics 支撑。

### 8.5 显式重试

显式整体 Tool 重试：

```text
保留 task_id
新建 request_id
新建 tool_run_id
旧 ToolRun、Asset、ToolResult、diagnostics 和错误保持不可变
```

新 ToolRun 独立产生 seed、图片、结果和错误，不得把旧尝试图片与新尝试性能拼接。Task 只在结果提交后显式更新 selected 引用。

`NEEDS_INPUT` 恢复保留 `task_id`、新建 `request_id`，补充完整并重新通过全部校验后才创建新的 `tool_run_id`。解释重试新建 request/explanation/LLM 调用记录，不重新执行 Tool。

## 9. Asset、ToolResult、Explanation 与溯源

### 9.1 Asset

单张生成 SEM 是正式平台资产：

```text
asset_type: sem_image
source_type: generated
```

至少记录：

```text
asset_id
actor_id
task_id
tool_run_id
status
role
object_key（内部）
mime_type
width / height / bit_depth
sha256
created_at
```

只请求性能时，SEM 的 `role=intermediate`，但仍是正式可追溯资产。API 不公开 MinIO object key、bucket、宿主机路径或永久公开 URL。

资产生命周期采用：

```text
PENDING
AVAILABLE
FAILED
ORPHANED
```

稳定写入方案为：

```text
PostgreSQL Asset PENDING
→ 编码图片并计算 SHA-256
→ 上传 MinIO
→ PostgreSQL Asset AVAILABLE
```

### 9.2 正式 PNG 最小契约

```text
单张灰度 PNG
无坐标轴、标题、色条、白边或拼图
不静默 resize / crop / pad
按确认规则映射为 uint8
SHA-256 用于文件完整性检查
```

PNG filter、IDAT 分块、跨编码器逐字节复现承诺和编码器补丁版本与 Tool 版本绑定不属于阶段 0 公共架构，作为阶段 1B 实现与验收细节处理。

### 9.3 `ToolResult`

正式公共必填字段为：

```text
status
requested_outputs
completed_outputs
failed_outputs
data
artifacts
warnings
tool_id
tool_version
schema_version
provenance
error
```

`confidence`、`out_of_distribution`、`applicability` 等是可选能力字段。当前 ZTA35G Tool 没有正式支持时可以不返回，不得填充 `null` 冒充已实现能力，也不得编造数值。warnings 必须明确模型评价指标、训练数据版本和 OOD/适用性验证尚未完整确认。

力学性能结构为：

```json
{
  "yield_strength": {"value": 650.0, "unit": "MPa"},
  "elongation": {"value": 3.2, "unit": "%"}
}
```

示例数值只展示结构，不代表模型实际输出或评价结果。

### 9.4 `provenance`

provenance 至少允许关联：

```text
task_id
tool_run_id
input_revision
normalized_process_parameters
actual_runtime_parameters
generated_sem_asset_id
model_bundle_id（内部轻量身份）
weight_file_fingerprints（内部或受控运维视图）
compatibility_acceptance_record
```

公共响应不必向普通用户展示权重文件细节或模型选择能力。不得保存完整 Tensor、DenseNet 特征、SVR 中间值或模型激活。

### 9.5 `NaturalLanguageExplanation`

正式结果层级只有：

```text
ToolResult
NaturalLanguageExplanation
```

Explanation 只读取已持久化 ToolResult。解释失败不改变 ToolResult 或 ToolRun 的成功事实；Task 可为 `PARTIALLY_SUCCEEDED`，后续只重试解释并继续引用同一 `input_result_id`。

## 10. 版本与模型文件轻量溯源

### 10.1 正式版本字段

- `tool_version`：平台如何集成、校验、执行和组装该 Tool。
- `schema_version`：Tool 输入输出数据结构版本。

MVP 不使用正式 `model_version` 字段，不允许普通用户选择模型文件组合。

### 10.2 模型 bundle fingerprint

内部静态配置或兼容性记录可以包含：

```text
model_bundle_id
weight_files[].filename
weight_files[].sha256
compatibility_acceptance_record
default_runtime_parameters
runtime_output_encoding_summary
```

该 bundle 身份只用于加载正确文件、完整性检查和事故排查。权重或依赖变化需要重新形成 fingerprint 和兼容性验收记录，但不扩展成发布、升级、灰度、回滚或在线动态选择系统。

## 11. 双环境与本地 Tool Runtime 兼容边界

### 11.1 已知原始模型环境

当前掌握的原始运行环境为：

```text
python==3.8
numpy==1.22.3
joblib==1.4.2
matplotlib==3.2.2
torch==1.13.1+cu116
torchvision==0.14.1+cu116
```

两个 SVR 的序列化环境还涉及：

```text
scikit-learn==1.0.2
```

这些信息仍需阶段 1B 实际验证。

### 11.2 默认环境决定

MVP 默认使用两个隔离 Conda 环境，不再把合并环境作为阶段 1 优先选择：

```text
Windows 宿主机
├─ Conda: materialsagent-backend
│  ├─ 候选 Python 3.11
│  ├─ FastAPI / LangChain / Pydantic
│  ├─ SQLAlchemy / Alembic
│  ├─ PostgreSQL driver / MinIO SDK
│  └─ 平台业务逻辑、未来登录和流式功能
├─ Conda: materialsagent-zta35g
│  ├─ Python 3.8
│  ├─ 原模型固定依赖
│  ├─ DDPM / DenseNet / SVR
│  └─ ZTA35G Tool Runtime
├─ Node.js
│  └─ Vue 3 + Vite
└─ Docker Compose
   ├─ PostgreSQL
   └─ MinIO
```

只有当模型明确验证可以迁移到后端 Python 版本，并且完整结果保持正确时，才重新评估合并环境；当前不以合并为目标。

### 11.3 本地 Runtime 定义

推荐调用链：

```text
materialsagent-backend
→ 本地 ZTA35G Tool Client Adapter
→ 127.0.0.1 上持续运行的 ZTA35G Tool Runtime
→ materialsagent-zta35g
```

约束：

1. Runtime 只监听本机回环地址，不作为公开 API。
2. 启动后加载模型并复用，不得每次请求重新加载全部权重。
3. 只接收已校验四维参数、`requested_outputs` 和必要 request context。
4. 返回 ToolExecutionOutput 等价协议，包括图片、性能结果、completed/failed outputs、错误和 diagnostics。
5. 不访问 PostgreSQL、MinIO、LangChain、前端或平台 Repository。
6. 后端继续拥有 Task、ToolRun、Asset、ToolResult 和 Explanation。
7. 平台仍是模块化单体；独立进程只是旧依赖兼容边界，不扩展为通用微服务治理。
8. 未来迁移远程 GPU 时，只替换 Tool Client Adapter 的配置或实现，不修改 Application、MaterialTool、ToolResult、数据库关系和前端。

Tool Runtime 的本地协议、进程管理、并发上限和传输格式属于阶段 1B 验证/实现细节。本节不预建服务发现、API Gateway、通用 RPC 框架或分布式追踪。

### 11.4 阶段 1A / 1B 待验证事项

阶段 1A 后续只需验证平台契约可在不加载真实模型时成立：

- Task、ToolRun、Asset、ToolResult、Explanation 和 selected 引用；
- StorageService/AssetService 方案 B；
- Mock MaterialTool 的一次 `execute` 契约、三种输出和部分成功；
- 结构化日志与 TaskProgressReporter 的最小实现；
- ActorContext、本地匿名主体、幂等和 `NEEDS_INPUT` 恢复；
- API 不暴露 object key。

阶段 1B 必须实际验证：

- Python 3.8 环境能否建立；
- PyTorch/Torchvision/CUDA、NumPy、Joblib、Matplotlib 是否可用；
- scikit-learn、SciPy、Pillow 等缺失版本；
- DDPM、DenseNet 和两个 SVR 权重加载；
- DDPM missing keys / unexpected keys；
- 完整单图生成和性能预测；
- GPU 驱动兼容性；
- 模型能否启动后加载一次并复用；
- 单张图像实际 shape、dtype、值域、尺寸和 uint8/PNG 映射；
- Tool Runtime 到本地 Client Adapter 的完整 ToolExecutionOutput；
- 可捕获性能失败时是否能返回已生成 SEM 和 diagnostics。

这些是未来验证门槛，不是本轮实施计划，也不表示已进入阶段 1A/1B。

## 12. 结构化日志与进度报告

### 12.1 结构化日志

MVP 可观测性主要回答：

```text
用户输入后发生了什么
参数在哪一步变化
Tool 是否调用
Tool 内部哪个主要步骤失败
图片是否保存
结果是否入库
解释是否生成
最终响应是否返回
```

每条关键日志至少允许记录：

```text
timestamp
request_id
conversation_id
task_id
tool_run_id（适用时）
actor_id
component
step
status
duration_ms
error_code
safe_error_message
```

关键 step 至少包括：

```text
request_received
intent_parsed
input_normalized
input_validated
tool_started
sem_generation
mechanical_property_prediction
tool_completed
asset_persistence
result_persistence
explanation_generation
response_completed
```

禁止记录完整 Tensor、图片 bytes、完整 Prompt、密钥或敏感用户数据。MVP 不设 Observability/Telemetry Port，不要求 OpenTelemetry、LangSmith、Metrics、告警或完整 Trace/Span 数据模型。未来接入 OpenTelemetry 时，把 request/task/tool run IDs 作为 Trace 属性即可。

### 12.2 `TaskProgressReporter`

为未来 SSE 保留唯一最小进度边界：

```text
report(
  task_id,
  tool_run_id,
  step,
  status,
  message,
  progress
)
```

`tool_run_id` 和 `progress` 可以为空。MVP 只允许：

```text
NoOpTaskProgressReporter
或 StructuredLoggingTaskProgressReporter
```

当前不持久化每条进度，不保证顺序、补发、重放或可靠投递。日志用于排错；ProgressReporter 用于未来向用户展示实时进度。未来 SSE/InProcess Streaming Adapter 可以消费同一边界，而不要求当前实现 Redis、Worker、WebSocket、EventPublisher 或 Event Store。

## 13. 未来扩展而不提前建设

### 13.1 新增 Tool

原则上只需：

1. 新增 Tool；
2. 新增输入输出 Schema；
3. 按需新增内部 Adapter；
4. 注册到代码级 Tool Registry；
5. 添加契约测试和展示支持。

MaterialTool、统一 ToolResult、ToolRun、diagnostics、StorageService 和 AssetService 继续复用。平台不假设新 Tool 有模型、GPU 或相同内部步骤。

### 13.2 SSE

未来增加 SSE 时，新增消费 TaskProgressReporter 的流式 Adapter。Task、ToolRun、Asset 和 ToolResult 仍是事实来源；不需要重写 Tool 执行契约或数据库核心关系。

### 13.3 登录系统

未来认证层构造真实 `ActorContext(actor_id, user_id)` 并增加访问控制检查。现有 Conversation、Task、Asset、Result 的 `actor_id` 关联继续使用，不重写 Tool 和结果核心关系。

## 14. 最小验收场景

1. 合法输入只请求 `sem_image`：一次 ToolRun、一次 Tool.execute、一张 AVAILABLE SEM、成功 ToolResult。
2. 只请求 `mechanical_properties`：Tool 返回性能和中间 SEM；Application 先保存 SEM，再提交性能成功结果。
3. 同时请求两项：只生成一张 SEM，ToolResult 同时返回图片和性能。
4. SEM 生成失败：diagnostics 能定位 `sem_generation`，没有成功资产或性能结果。
5. 同时请求两项，SEM 已返回但性能失败：图片保存成功后 ToolRun/Task 可部分成功。
6. 只请求性能但性能失败：内部 SEM 保留，用户目标未完成，ToolRun/Task 失败。
7. Tool 返回性能成功但图片保存失败：不提交不可追溯的性能成功结果。
8. ToolResult 成功而 Explanation 失败：ToolRun 成功、Task 部分成功，只重试 Explanation。
9. 缺少输入：Task `NEEDS_INPUT`，不创建 ToolRun；补充后重新全量校验。
10. 显式 Tool 重试：保留 task_id，新建 request_id/tool_run_id，旧运行不可变。
11. 结构化日志能用 request_id/task_id/tool_run_id 串联主要链路。
12. TaskProgressReporter 丢失进度不改变持久化业务事实。
13. API 不暴露 MinIO object key，Explanation 不覆盖 ToolResult。
14. 当前 ZTA35G Tool 不支持的 confidence/OOD/applicability 字段不编造，由 warnings 表达验证限制。
15. 新增第二个非模型 Tool 时不需要修改现有 ZTA35G Tool、StorageService、TaskService 或公共 ToolResult。
16. 本地 Runtime 关闭或不可用时错误定位在 Tool 调用/可用性大环节，不扩散旧模型依赖到 backend 环境。

## 15. 设计一致性检查

本文明确删除或不再使用：

```text
model_version 正式字段及发布生命周期
InferenceRun / inference_run_id
active_inference_run_id / selected_inference_run_id
PredictorRawOutput 独立实体
StageRun 独立公共实体或强制表
Application 两阶段调用 Predictor
GeneratedSEMRawPayload 跨阶段身份
Remote 临时张量 token
完整 EventPublisher / TaskEvent / sequence / 重放设计
Observability / Telemetry Port
TaskExecutor Port
project_id / tenant_id / 角色矩阵
强制状态转换历史表
CANCELLED / TIMED_OUT 预留状态
```

本文继续保留：

```text
MaterialTool / Tool Registry / Tool Catalog
ToolRun diagnostics
StorageService / AssetService
TaskProgressReporter
ActorContext(actor_id, user_id)
双 Conda 环境
本地 ZTA35G Tool Runtime 兼容边界
Task / ToolRun / Asset / ToolResult / Explanation
```

本文件已通过项目负责人复核，确认为阶段 0 第一节设计基线。后续章节如发现真实逻辑冲突，应先提出修改建议，经项目负责人确认后再修改本基线。
