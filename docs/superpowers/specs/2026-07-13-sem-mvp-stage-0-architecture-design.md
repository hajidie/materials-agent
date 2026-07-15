# 材料智能体平台阶段 0：ZTA35G SEM Virtual Lab MVP 范围与总体架构

> 状态：已确认架构基线
>
> 修订日期：2026-07-15
>
> 适用范围：阶段 0 文档设计
>
> 事实依据：对 `SEM/` 目录进行静态、只读检查，并结合项目负责人于 2026-07-14 和 2026-07-15 确认的模型与平台业务契约；未导入模型、未加载权重、未执行训练或推理。

## 1. 本次修订结论

第一阶段将 SEM 图像生成与力学性能预测设计成同一个复合能力。当前源码本身是一个耦合的虚拟实验室链路，性能预测依赖四维工艺参数和模型内部生成的 SEM 图像特征。因此 MVP 只注册一个材料范围明确的复合 Tool，并通过一个复合 Predictor Port 调用同一模型组合。具体 Adapter 由第 12.6 节的环境验证结果决定：

```text
tool_id: zta35g_sem_virtual_lab
Tool: ZTA35GSEMVirtualLabTool
Predictor Port: ZTA35GSEMVirtualLabPredictor
方案 A Local Adapter: LocalZTA35GSEMVirtualLabPredictor
方案 B Remote Adapter: RemoteZTA35GSEMVirtualLabPredictor
```

两种方案共享同一个 Predictor Port、Tool 和结果契约，不产生第二个 Predictor Port 或第二个 Tool。

该工具仅适用于 **ZTA35G 钛合金**。工具名称、说明、Tool Catalog 元数据和错误信息都不得暗示其适用于任意材料体系。

此前的拆分式 Tool/Predictor 架构、跨 Tool 资产串联、上传 SEM 图像预测入口及分别记录 Tool 运行的方案全部失效。真实 SEM 图像的尺寸、标尺、设备和域适配也不属于 MVP 输入要求。

新的统一方向为：

```text
一个 ZTA35G SEM Virtual Lab 复合 Tool
→ 一个复合 Predictor
→ 根据 requested_outputs 决定执行到哪个内部阶段
```

## 2. SEM 原始目录只读检查事实

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

| 类别 | 当前内容 | 结论 |
|---|---|---|
| 训练代码 | 未发现训练循环、优化器、损失计算或模型 `fit` 流程 | 当前仓库不包含可确认的训练代码 |
| 模型结构 | `virtual_lab_sem.py` 中的条件 DDPM U-Net；Torchvision DenseNet121；两个序列化 SVR | 已确认 |
| 推理代码 | `VirtualLab.generate_images`、`extract_features`、`predict_performance`、`run` | 已确认，当前耦合在一个类和一个完整流程中 |
| 模型迁移脚本 | `Update_SVR_model.py` 加载并覆盖保存两个 Joblib 模型 | 不是训练或正式推理代码；会改写权重，阶段 0 不运行 |
| 数据 | `initial_para.csv` 和 `train_images/` | 缺少训练脚本和正式数据说明；MVP 不建设训练数据版本管理 |

### 2.2 权重加载事实

| 模型 | 权重 | 当前加载方式 | 风险 |
|---|---|---|---|
| 条件 DDPM U-Net | `ddpm_512_epoch_800.pth` | `torch.load`；优先 `ema`，其次 `model`；清理键前缀；`strict=False` | 可能静默忽略 missing/unexpected keys，必须在阶段 1 验证 |
| DenseNet121 | `densenet121-a639ec97.pth` | 构造 `densenet121(weights=None)`；转换旧版 denselayer 键名后加载 | PyTorch/Torchvision 兼容组合未知 |
| 屈服强度 SVR | `Final_Yield_Strength.pkl` | `joblib.load` | 序列化元数据为 scikit-learn 1.0.2 |
| 延伸率 SVR | `Final_Elongation.pkl` | `joblib.load` | 序列化元数据为 scikit-learn 1.0.2 |

### 2.3 已确认的模型业务契约

模型只适用于 ZTA35G 钛合金。四维工艺参数定义如下：

| 代码参数 | Tool 输入字段 | 正式业务名称 | 类型与精度 | 标准单位 | 允许范围 |
|---|---|---|---|---|---|
| `T1` | `solution_temperature` | 固溶温度 | 整数 | °C | 900–1100 |
| `t1` | `solution_time` | 固溶时间 | 数值，最多 1 位小数 | h | 1–5 |
| `T2` | `aging_temperature` | 时效温度 | 整数 | °C | 670–790 |
| `t2` | `aging_time` | 时效时间 | 数值，最多 1 位小数 | h | 1–5 |

平台不得把业务字段名称直接暴露为含义不明确的 `T1/t1/T2/t2`，但 Predictor Adapter 可以在边界内把正式业务字段映射为源码所需顺序。

以上类型和精度是当前阶段 0 的业务输入契约。已有静态检查记录未确认模型代码还存在其他小数位硬限制；阶段 1B 必须验证标准化后的整数温度和最多 1 位小数时间能够沿现有模型链路正确传递。若实测发现额外硬性要求，应先修订契约和验收记录，不得在阶段 0 修改模型代码或静默改变用户输入。

当前正式内部流程为：

```text
四维工艺参数
→ 条件 DDPM 生成一张单通道 SEM 图像
→ DenseNet121 提取图像特征
→ 图像特征与四维原始工艺参数拼接
→ 屈服强度 SVR + 延伸率 SVR
→ 屈服强度（MPa）+ 延伸率（%）
```

性能预测必须携带四维工艺参数，不能设计为只使用 SEM 图像预测。MVP 不支持用户上传真实 SEM 图像进行性能预测。性能预测只使用本次复合 Predictor 内部生成的单张 SEM 图像，不进行多图特征平均。

## 3. 阶段边界与 MVP 范围

### 3.1 阶段 0

本文件属于阶段 0。本阶段只建立项目章程、需求边界、总体架构、技术决策和开发规范。

阶段 0 不执行以下工作：

- 修改、移动、删除或运行 `SEM/` 原始代码；
- 提取或重构模型推理代码；
- 创建 FastAPI、数据库表或 Alembic migration；
- 创建 Docker Compose、启动容器或安装基础服务；
- 实现 Tool、Predictor Adapter、Tool Registry 或 LangChain；
- 实现前端或模型推理服务；
- 安装 Python、Node.js 或模型依赖；
- 验证权重加载或运行完整模型链路。

### 3.2 第一阶段 MVP

第一阶段只接入一个复合 Tool：

```text
zta35g_sem_virtual_lab
```

它支持三个请求模式：

1. 只返回生成 SEM 图像；
2. 只返回力学性能，但内部仍生成并保存中间 SEM 图像；
3. 同时返回生成 SEM 图像和力学性能。

第一阶段仍使用 LangChain，不使用 LangGraph。由于只有一个工具和一个确定的内部阶段链路，不引入通用工作流引擎、多智能体或动态 DAG。

### 3.3 MVP 非范围

第一阶段 MVP 暂不实现：

- 用户上传真实 SEM 图像预测力学性能；
- 拆分式 SEM Tool/Predictor 架构；
- EBSD 图像生成和 EBSD 性能预测；
- 图像分割、裂纹检测或其他材料工具；
- LangGraph、多智能体和通用工作流引擎；
- Redis、后台任务队列和可靠事件投递；
- SSE 和 WebSocket；
- 在兼容性验证前预先建设独立模型服务；若阶段 1B 证明依赖不兼容，只允许按第 12.6 节评估最小独立推理进程或服务；
- GPU 容器、GPU 集群和自动扩缩容；
- 完整可观测平台和完整评价体系；
- 用户、权限、计费和多租户；
- 模型训练和数据标注平台；
- 动态插件安装或数据库动态注册；
- 完整模型版本管理平台、训练数据版本、模型别名、灰度发布和在线回滚；
- 生产级容器部署。

“不进行生产级容器部署”不等于完全不使用 Docker。阶段 1 计划仅通过 Docker Compose 管理 PostgreSQL 和 MinIO；FastAPI、LangChain 和模型验证/推理进程暂时运行在 Windows 宿主机，后端与模型是否合并环境由第 12.6 节决定。

## 4. 复合 Tool 输入契约

### 4.1 用户业务输入

面向用户和 Application 的输入只包含业务必需信息：

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

`requested_outputs` 是非空集合，只允许：

```text
sem_image
mechanical_properties
```

处理规则为：

1. 空集合使用 `INVALID_REQUESTED_OUTPUT` 拒绝；
2. 包含未知枚举值时使用 `INVALID_REQUESTED_OUTPUT` 拒绝；
3. 重复的合法枚举值由 Application 确定性去重，首次出现顺序可以保留；
4. 去重不改变执行语义，也不导致同一内部阶段重复执行；
5. 原始 requested outputs 和规范化后的 requested outputs 均可记录，用于排查 LangChain 参数提取问题。

例如：

```json
["sem_image", "sem_image"]
```

规范化为：

```json
["sem_image"]
```

合法枚举去重属于输入标准化，不属于静默补全关键业务参数。

ZTA35G 是该 Tool 的固定适用材料，不是可自由替换的模型参数。自然语言任务明确涉及其他材料时，Application 必须拒绝并返回 `UNSUPPORTED_MATERIAL`；材料意图不明确且可能影响工具选择时，LangChain 应追问，不能自动泛化该工具。

### 4.2 参数校验

Application 和 Tool 必须在 Predictor 调用前执行确定性校验：

- 四个工艺参数全部必填，不得由 LLM 或代码静默补全；
- `solution_temperature` 和 `aging_temperature` 的标准化值必须是整数，不接受带非零小数部分的值；原始 `1000.0` 可以在保留原始表示的前提下无损规范化为整数 `1000`，`1000.5` 必须拒绝；
- `solution_time` 和 `aging_time` 必须是数值，且最多包含 1 位小数；
- 固溶温度必须位于 900–1100 °C；
- 固溶时间必须位于 1–5 h；
- 时效温度必须位于 670–790 °C；
- 时效时间必须位于 1–5 h；
- 不得自动截断、四舍五入或改变用户输入来满足类型、精度或范围；
- 超出范围必须拒绝，不采用“警告后继续推理”；
- 不得在缺少标准化记录时静默改变单位、交换字段顺序或把缺失值替换为默认值。

越界错误码：

```text
PROCESS_PARAMETERS_OUT_OF_RANGE
```

错误详情至少包含：

```json
{
  "parameter": "solution_temperature",
  "actual_value": 1150,
  "allowed_min": 900,
  "allowed_max": 1100,
  "unit": "°C"
}
```

缺失参数使用 `MISSING_PROCESS_PARAMETER`，非法输出枚举使用 `INVALID_REQUESTED_OUTPUT`。错误响应必须携带适用的 `request_id`、`task_id` 和 `trace_id`。

超出类型或精度契约时使用：

```text
INVALID_PROCESS_PARAMETER_PRECISION
```

错误详情至少包含：

```json
{
  "parameter": "solution_time",
  "actual_value": 3.25,
  "expected_type": "number",
  "allowed_precision": "最多 1 位小数",
  "unit": "h"
}
```

### 4.3 自然语言单位标准化边界

Tool 的标准内部单位保持不变：温度使用 °C，时间使用 h。LangChain 可以从自然语言中提取原始数值和原始单位，但其输出只是候选参数，不能独立完成未经校验的最终换算。

Application 在 Tool 校验前使用最小的 `UnitNormalization` / `DeterministicParameterNormalization` 边界执行确定性标准化。MVP 只覆盖本 Tool 实际需要的单位：温度接受明确的 °C，时间接受明确的 h 和 min，并可按 `60 min = 1 h` 转换；不建设通用科学单位平台，也不引入重量级单位依赖。

标准化记录应同时保留：

- 用户原始数值和原始单位；
- 标准化后的数值和标准单位；
- 使用的确定性转换规则；
- 转换状态或结构化错误。

例如“固溶 1000 °C，持续 180 min”可标准化为 `solution_temperature=1000 °C`、`solution_time=3.0 h`。转换完成后仍必须按第 4.2 节执行类型、精度和范围校验；如果分钟换算结果超过 1 位小数，不得四舍五入，应返回 `INVALID_PROCESS_PARAMETER_PRECISION`。

不支持、缺失或存在歧义的单位使用 `INVALID_PROCESS_PARAMETER_UNIT` 返回结构化错误，错误详情至少包含 `parameter`、`actual_value`、`actual_unit` 和 `supported_units`；也可以在无法形成确定输入时使任务进入 `NEEDS_INPUT`。LLM 不得猜测单位。该边界可以是 Application 内部的确定性组件或规则集，不要求建设独立服务。

### 4.4 平台模型配置

以下内容属于平台配置，不直接作为普通用户业务参数：

- `num_samples`：MVP 固定为 1；
- `guide_scale`：MVP 平台固定为当前代码默认值 2.0；
- `timesteps`：MVP 固定为当前模型配置 1000；
- seed 策略：MVP 由平台生成一个可记录的 32 位无符号整数并传给 Predictor；
- 模型权重路径或模型包定位信息；
- 设备和并发策略。

普通用户不能覆盖 `num_samples`、`guide_scale`、`timesteps`、权重路径或设备。以后若为高级用户开放 seed，应通过兼容的 Schema 变更显式加入。

### 4.5 实际推理运行参数

每次执行必须记录实际使用值，而不是只记录默认配置：

```text
num_samples = 1
guide_scale = 实际值
timesteps = 实际值
seed = 实际值
device = 实际设备
```

无论 seed 由平台生成还是以后由高级用户指定，都必须保存实际 seed。固定 seed 是否能在不同硬件和依赖版本间实现可接受复现，需要在阶段 1 验证，阶段 0 不作保证。

## 5. 复合 Predictor 内部执行规则

### 5.1 请求 `sem_image`

```text
校验四维工艺参数
→ sem_generation
→ 生成一张 SEM 图像
→ 返回生成图像原始输出
→ mechanical_property_prediction = SKIPPED
→ 不执行 DenseNet 和两个 SVR
```

生成图像必须保存为正式平台资产并返回。

### 5.2 请求 `mechanical_properties`

```text
校验四维工艺参数
→ sem_generation
→ 生成一张 SEM 图像
→ DenseNet121 特征提取
→ 特征与四维工艺参数拼接
→ 两个 SVR 预测
→ 返回屈服强度和延伸率
```

即使用户只请求力学性能，内部生成的 SEM 图像也必须保存为中间资产并获得独立 `asset_id`。前端可以不把它作为主要结果展示，但 ToolResult、运行记录和性能结果必须能够追溯到该资产。

### 5.3 同时请求两项

执行完整链路，返回生成 SEM 图像和力学性能。图像生成只执行一次，性能阶段复用同一张生成图像，不得为不同输出重复生成。

### 5.4 Predictor Port 的部分输出契约

复合 Predictor 不依赖 PostgreSQL、MinIO、StorageService、Tool Registry、LangChain 或 HTTP。Local/Remote Predictor Adapter 都应返回相同的阶段化复合结果，至少能够表达：

- `sem_generation` 在第 11.1 节 Stage 状态集合中的状态；
- 成功时的单张图像载荷或可由 Tool 持久化的中立表示；
- `mechanical_property_prediction` 在第 11.1 节 Stage 状态集合中的状态；
- 成功时的屈服强度和延伸率原始值；
- 失败阶段、错误类别和可安全记录的信息；
- 实际运行参数。

如果图像生成成功但性能阶段发生可捕获异常，Predictor Adapter 必须把已生成图像随阶段化失败结果返回，使 ToolExecutionService 能通过 AssetService/StorageService 保存它。不得把 StorageService 注入模型代码来换取中间资产保存。

MVP 对进程被强制终止、宿主机崩溃或显存进程直接退出时的中间资产可靠保存不作保证；可靠阶段检查点属于后续队列和独立推理服务演进范围。

## 6. 模块化单体总体架构

```text
Vue 3 + Vite Frontend
   ↓
FastAPI API（未来公共路径 /api/v1）
   └─ 建立 ActorContext / ExecutionContext
   ↓
Application Services
   ├─ ChatOrchestrationService
   │    └─ LangChain Orchestrator
   │          └─ Tool Catalog View
   │                └─ derived from Tool Registry metadata
   ├─ RunZTA35GSEMVirtualLabUseCase
   ├─ ToolExecutionService
   ├─ AssetService
   ├─ TaskService
   └─ ResultService
          ↓
Core Ports / Contracts / Registries
   ├─ MaterialTool Contract
   ├─ Tool Registry
   │    └─ ZTA35GSEMVirtualLabTool
   │          └─ ZTA35GSEMVirtualLabPredictor Port
   ├─ TaskExecutor Port
   ├─ StorageService Port
   ├─ Repository Ports
   ├─ EventPublisher Port
   └─ Observability / Telemetry Port
          ↓
Infrastructure Adapters
   ├─ PostgreSQL Repositories
   ├─ MinIO StorageService
   ├─ LocalZTA35GSEMVirtualLabPredictor（方案 A）
   ├─ RemoteZTA35GSEMVirtualLabPredictor（方案 B）
   ├─ SyncTaskExecutor 或 InProcessTaskExecutor
   ├─ InProcessEventPublisher
   └─ StructuredLoggingTelemetry
```

### 6.1 Application 不是巨型类

- `ChatOrchestrationService`：取得 LangChain 的结构化意图，决定知识回答还是允许的 Application Use Case。
- `RunZTA35GSEMVirtualLabUseCase`：协调一次复合 Tool 用例，不实现模型细节。
- `ToolExecutionService`：解析 Tool、创建运行记录、调用 Tool 并处理阶段化结果。
- `AssetService`：通过 StorageService 保存和登记生成资产。
- `TaskService`：创建任务并执行状态迁移。
- `ResultService`：保存 PredictorRawOutput 摘要、ToolResult 和自然语言解释。

Application 内部还预留最小的 `UnitNormalization` / `DeterministicParameterNormalization` 边界，负责第 4.3 节的确定性单位转换和输入规范化。它可以是小型组件或规则集，不作为独立微服务，也不建设通用科学单位平台。

这些服务通过 `ExecutionContext` 关联执行链路，并从其中读取当前 `ActorContext`、追踪标识和已解析的版本上下文；不得为新增追踪字段持续扩张每个服务的方法签名。底层服务不得反向依赖 `ChatOrchestrationService`。复合模型内部阶段不应扩展成通用 WorkflowOrchestrationService。

### 6.2 LangChain 与 Application 边界

LangChain 只负责：

- 理解是否需要调用 `zta35g_sem_virtual_lab`；
- 从自然语言提取四维工艺参数的原始数值和原始单位建议；
- 提议 `requested_outputs`；
- 识别明显缺失的输入或歧义；
- 必要时生成追问。

LangChain 只返回稳定 `tool_id` 和结构化参数建议，不选择 `tool_version`、`model_version`，不直接执行函数，也不得绕过 Application 构造任意内部阶段调用。

Application 负责：

- 确定性规范化受支持单位和重复的合法 requested outputs，并保留原始值与规范化值；
- 对材料范围、参数、单位、范围和 requested outputs 再次校验；
- 通过 Tool Registry 解析启用的 `tool_version`；
- 通过平台版本策略解析 `model_version`；
- 根据 requested outputs 决定复合 Predictor 执行到哪个内部阶段；
- 创建 task、tool_run、inference_run、asset 和 result 记录；
- 保存中间 SEM 图像；
- 处理失败与部分成功；
- 阻止 LLM 编造图像、性能值、置信度或评价指标。

### 6.3 Tool Registry 与 Tool Catalog

Tool Registry 是工具元数据和可执行 Tool 的唯一事实来源。MVP 使用代码级静态注册，不实现动态插件安装或数据库注册。

Registry 的通用元数据至少允许表达：

```text
enabled
input_schema
output_schema
supported_asset_types
supported_outputs
execution_mode
requires_gpu
timeout_seconds
availability
```

其中 `enabled` 是平台配置是否允许使用，`availability` 是依赖、健康检查和兼容性验收共同决定的当前可用状态；两者不得混为同一字段。`schema_version` 仍是 Tool 输入/输出契约版本，不因为增加 Registry 元数据而新增另一套 Tool 版本字段。

Tool Catalog 是从 Registry 元数据生成的只读 LangChain 投影，不单独维护或持久化。Catalog 中必须明确：

- 只适用于 ZTA35G 钛合金；
- 必需四维工艺参数；
- 支持的 requested outputs；
- 不接受用户上传 SEM 图像；
- 工艺范围和主要错误类型。

### 6.4 通用 `MaterialTool` 契约与扩展流程

当前 `ZTA35GSEMVirtualLabTool` 实现通用 `MaterialTool` 契约，但通用 Tool 不假设一定存在 Predictor 或机器学习模型。最小稳定契约为：

- `metadata`：返回 Registry/Catalog 所需的工具身份、Schema 和执行能力元数据；
- `validate_input`：执行确定性输入校验，返回已校验输入或结构化错误；
- `execute`：在 `ExecutionContext` 下执行 Tool，返回统一 `ToolResult`；
- `health_check`：报告 Tool 当前依赖和执行路径是否可用，不把健康检查等同于业务推理；
- 统一 `ToolResult`：无论 Tool 依赖 Predictor、规则计算、外部设备 Adapter 或其他执行方式，都使用第 9 节的结果边界。

Tool 可以依赖专用 Predictor Port 或其他执行 Adapter，但不能要求 Application 了解这些内部依赖。新增 Tool 的标准流程原则上只包括：

1. 新增 Tool 及其专用输入/输出 Schema；
2. 按需新增 Predictor 或其他执行 Adapter；
3. 在代码级静态 Tool Registry 中注册；
4. 添加 MaterialTool、Schema、错误和 ToolResult 契约测试；
5. 添加必要的 Application Use Case 和结果展示。

正常新增 Tool 不应修改 `StorageService`、`TaskService`、Event 公共协议或现有 Tool。若新增需求确实暴露公共契约缺口，应先单独评审公共契约变更，不能把工具特例直接写入公共服务。

### 6.5 `TaskExecutor` Port

`TaskExecutor` 是 Application 拥有的执行方式边界。Application Use Case 只提交包含 `task_id`、任务类型、已校验业务载荷和 `ExecutionContext` 的任务命令，并根据统一回执或任务状态工作，不依赖当前是在请求进程内同步执行还是由 Worker 异步执行。

MVP 可使用 `SyncTaskExecutor` 或 `InProcessTaskExecutor`。未来可以替换为 `QueueTaskExecutor`，再由 Redis/队列和 Worker 执行同一类任务命令；替换时不改变 Tool、ToolResult、Task 状态协议或 Application Use Case 的业务规则。本阶段只定义职责、最小命令边界和依赖方向，不设计队列拓扑、重试中间件或 Worker 部署。

### 6.6 文件与数据库边界

所有文件必须通过 StorageService 访问：

- MinIO 保存生成 SEM 图像和其他结果文件本体；
- PostgreSQL 保存资产元数据、对象键、任务、运行、结果、错误、版本和溯源关系；
- Predictor 不接触 MinIO、本地平台目录或数据库；
- 业务层不得直接使用 MinIO SDK；
- 不得把宿主机绝对路径保存为资产引用。

### 6.7 Event、发布与流式订阅边界

Event 表示业务状态变化和可供未来 SSE 消费的通知，不等同于日志、Trace 或 Metric。`EventPublisher` 只负责发布版本化 `TaskEvent`；它不是完整可观测性实现。

`TaskEvent` 的最小公共字段为：

```text
event_id
event_type
schema_version
sequence
task_id
trace_id
timestamp
progress
message
data
```

`sequence` 在同一任务事件流内用于有序消费；`progress`、`message` 和 `data` 按事件类型可空。`data` 只携带必要的小型结构化信息以及 `result_id`、`asset_id` 等引用，不内嵌大型结果或文件。

这里的 `schema_version` 是 TaskEvent 公共协议版本，独立于 Tool 输入/输出的 `schema_version`；两者在持久化和日志中应使用清晰的字段路径或命名空间，避免混淆。

除模型内部阶段事件外，公共事件类型至少预留：

```text
task_created
task_started
task_completed
task_failed
assistant_message_started
assistant_message_delta
assistant_message_completed
result_available
artifact_available
```

复合运行仍可产生阶段级事件，例如：

```text
tool_run_started
sem_generation_started
sem_generation_succeeded / sem_generation_failed
mechanical_property_prediction_started
mechanical_property_prediction_succeeded / mechanical_property_prediction_failed
tool_run_succeeded / tool_run_partially_succeeded / tool_run_failed
```

MVP 使用 `InProcessEventPublisher`，不实现 SSE，也不承诺临时事件可靠投递。未来由 `EventSubscription` 或 `TaskEventStream` 提供按 `task_id` 订阅的读取边界，SSE Adapter 只负责把任务状态流、LLM 文本增量流和结果/资产通知流转换为 HTTP 流式响应。Application 只依赖 `EventPublisher`，不依赖 Redis、SSE 或具体订阅实现。

PostgreSQL 持久化任务状态、tool/inference run、关键审计事件、资产和结果；不要求持久化每一条进程内进度事件或 `assistant_message_delta`。临时通知的可靠存储、补发和断点续传机制留到 Redis、队列或事件系统阶段决定。

### 6.8 Observability / Telemetry 边界

Observability 包括 logs、traces 和 metrics。MVP 只要求结构化日志以及贯穿 `ExecutionContext` 的追踪字段，并预留最小 Observability/Telemetry 边界，使 Application、Tool、Predictor Adapter、Storage Adapter 和日志系统使用同一组关联标识。

阶段 0 不定义完整遥测数据模型；稳定边界只要求：

- 结构化日志能够记录执行上下文、组件、动作、状态、错误码和安全错误摘要；
- 调用链能够传播 `trace_id`、`task_id`、`tool_run_id` 和 `inference_run_id`；
- 后续 Trace Span、Metric 和告警实现可以替换或扩展 Adapter，而不修改业务契约。

未来可按需要接入 LangSmith（LLM/Agent 调用追踪）、OpenTelemetry、指标和告警系统。业务 Event 可以成为遥测的输入信号之一，但 `EventPublisher`、日志记录器和 Trace/Metric Adapter 保持不同职责。

阶段 1A 的实际范围只包括结构化日志和 `ExecutionContext` 传播；不自建完整 Trace、Metric 或告警框架，也不要求接入 OpenTelemetry 或 LangSmith。

## 7. Actor、ExecutionContext、标识与运行关系

### 7.1 `ActorContext`

`ActorContext` 表示当前调用主体和未来的所有权/访问控制范围，最少预留：

```text
actor_id
user_id
project_id
tenant_id
```

MVP 可以使用本地匿名主体：生成或配置稳定的 `actor_id`，`user_id`、`project_id`、`tenant_id` 可以为空。Task、Asset 和 Result 的持久化边界仍应允许关联这些所有权字段，避免正式用户系统加入后重建全部溯源关系。

`ActorContext` 不是本阶段的认证、授权或多租户实现。当前只定义主体传递、所有权关联和未来访问控制检查的边界，不设计角色矩阵、租户路由或计费模型。

### 7.2 统一 `ExecutionContext`

Application、MaterialTool、Predictor Adapter、Storage/Repository Adapter、EventPublisher 和日志/Telemetry 系统通过统一 `ExecutionContext` 关联一次执行链路。最小上下文至少承载：

```text
request_id
conversation_id
task_id
trace_id
tool_run_id
inference_run_id
actor_id（或 ActorContext 引用）
time_context
version_context
```

`time_context` 至少能够表达请求接收时间、当前执行开始时间和可选截止时间；`version_context` 至少能够表达公共 API 版本，并在解析后携带 `tool_version`、`model_version`、`schema_version` 和适用的 prompt template 版本。业务实体自己的 `created_at`、`started_at`、`completed_at` 仍分别持久化，不能只依赖上下文中的时间。

上下文按执行层级派生：HTTP 入口先建立 request/trace/actor 信息，创建任务后补充 `task_id`，开始 Tool 和 Predictor 调用时分别派生带 `tool_run_id`、`inference_run_id` 的子上下文。尚未创建的标识可以为空，但字段位置和传播方式保持稳定。不得把这些不同标识合并成一个通用 ID，也不得为每次新增追踪字段修改所有业务函数签名。

### 7.3 标识定义

- `request_id`：一次 HTTP 请求；重试 HTTP 请求会产生新的 request_id。
- `conversation_id`：一组关联的多轮交互。
- `task_id`：稳定业务任务，不等同于请求或执行尝试。
- `trace_id`：一次实际端到端执行链路。
- `tool_run_id`：任务中一次 `zta35g_sem_virtual_lab` 逻辑执行。
- `inference_run_id`：一次实际复合 Predictor 调用，也是重试尝试的来源边界。
- `asset_id`：上传、生成或派生文件资产；本 MVP 只使用模型生成 SEM 资产。
- `result_id`：一个持久化结果聚合，必须能定位其实际来源 `inference_run_id`。

正常成功路径为：

```text
task_id / trace_id
└─ tool_run_id: zta35g_sem_virtual_lab
   └─ inference_run_id: composite inference
      ├─ stage: sem_generation
      │  └─ generated sem asset_id
      └─ stage: mechanical_property_prediction（按 requested_outputs 决定是否执行）
         └─ ToolResult result_id
```

MVP 不把内部阶段登记成 Tool，也不为各阶段分别创建 `tool_run_id`。

### 7.4 重试与结果来源

一个逻辑 `tool_run_id` 可以因整体重试关联多个 `inference_run_id`。每个 inference run 独立记录状态、开始/结束时间、运行参数、阶段状态和错误，不能用后一次尝试覆盖前一次记录。

```text
tool_run_id
├─ inference_run_id A: FAILED
│  ├─ error A
│  └─ 可选的已生成资产 A（仍标记来源 A，不作为最终成功来源）
└─ inference_run_id B: SUCCEEDED
   ├─ generated asset B
   └─ result_id B
      └─ task/tool_run 的最终结果明确指向 B
```

所有 asset 和 result 都必须关联实际产生它们的具体 `inference_run_id`。最终 `result_id` 除 `task_id`、`tool_run_id` 外必须保存来源 `inference_run_id`；失败尝试的错误、部分阶段和资产可以保留用于审计，但不得被误认为最终成功结果来源，也不得把尝试 A 的资产与尝试 B 的性能值拼成未声明的聚合结果。

### 7.5 阶段记录

复合 `inference_run` 必须记录内部阶段状态，但无需在阶段 0 决定具体数据库表结构。每个阶段至少预留：

- 阶段名；
- 开始和结束时间；
- 状态；
- 错误码和安全错误摘要；
- 所属 `inference_run_id`；
- 关联 `asset_id` 或 `result_id`；
- 实际运行配置；
- `tool_version`、`model_version`、`schema_version`。

## 8. 资产与溯源设计

### 8.1 生成 SEM 图像资产

单张生成图像是正式资产：

```text
asset_type: sem_image
source_type: generated
```

至少记录：

- `asset_id`；
- `actor_id` 以及适用的 `user_id`、`project_id`、`tenant_id`；
- `task_id`；
- `tool_run_id`；
- `inference_run_id`；
- 四维工艺参数及单位；
- 实际 seed；
- 实际 guide scale；
- 实际 timesteps；
- 图像尺寸、MIME type 和位深；
- `model_id`；
- `model_version`；
- `tool_version`；
- `object_key`；
- 内容哈希或校验值；
- `created_at`。

MVP 固定 `num_samples=1`。不得把多图拼图作为正式模型结果。若以后额外生成可视化拼图，它只能是 `asset_type: visualization`、`source_type: derived` 的派生资产，不能替代原始单张 SEM 资产。

正式生成资产的最小保存契约为：

1. 每次执行只保存一张独立 SEM 图像作为该次生成的正式资产；
2. 正式资产建议使用单通道灰度 PNG，MIME type 为 `image/png`；
3. 图像尺寸使用模型实际输出尺寸，当前预期为 512×512，阶段 1B 必须实测确认；
4. Predictor 原始值域为 `[-1, 1]`，保存前必须按 manifest 中固定的规则确定性映射到像素值；
5. 映射、裁剪、量化、位深和 PNG 编码规则必须记录为模型包运行配置或资产生成元数据；
6. 不保存带坐标轴、标题、色条、白边或多图拼图的 Matplotlib 可视化作为正式模型结果；
7. 默认不保存完整浮点张量；未来确需保留时，只能作为单独派生资产或受控调试产物，不替代正式 PNG；
8. MIME type、实际宽高、位深和内容哈希必须进入资产元数据。

图像后处理规则会影响结果复现，因此属于由 `model_version` 固定、在 model manifest 中管理的运行输出契约。本阶段不实现 PNG 编码；具体映射和编码步骤留到第二节明确。

`object_key` 只能作为 StorageService 和受控下载接口使用的内部存储引用，不能直接作为永久公开 URL 返回前端。未来由鉴权下载接口或短期签名 URL 提供资产访问；MVP 即使使用本地匿名主体，也必须保持该边界。

### 8.2 力学性能结果溯源

性能结果必须关联：

- 内部生成 SEM 图像的 `asset_id`；
- 四维工艺参数及单位；
- `tool_id`、`tool_version`；
- `model_version`；
- `schema_version`；
- `task_id`、`trace_id`、`tool_run_id`、`inference_run_id`、`result_id`。

这里的 `inference_run_id` 必须是实际产生该性能值和关联 SEM 资产的执行尝试。`result_id` 不能只挂在逻辑 `tool_run_id` 下而丢失具体来源；Task 的最终结果引用必须明确选择哪个 inference run 产生的结果。

完整溯源链：

```text
四维工艺参数
→ zta35g_sem_virtual_lab tool_run
→ composite inference_run
→ sem_generation stage
→ generated SEM asset_id
→ mechanical_property_prediction stage
→ mechanical-property result_id
→ NaturalLanguageExplanation
```

即使用户只请求力学性能，也不得丢弃中间 SEM 图像、seed、生成配置或工艺条件。

### 8.3 `AssetService` 生命周期与一致性边界

`AssetService` 的当前简单职责是通过 StorageService 保存、登记和读取资产。其稳定接口还应允许未来增加：

- 删除与受控访问；
- 保留策略和到期处理；
- 内容哈希/完整性检查；
- PostgreSQL 元数据与 MinIO 对象的一致性处理；
- 孤儿对象和孤儿元数据清理；
- 备份与恢复协调边界。

阶段 0 不决定软删除期限、事务补偿算法、清理调度、备份介质或灾难恢复指标。Application 和 Tool 只依赖 AssetService/StorageService 稳定边界，不直接实现 MinIO 与 PostgreSQL 的一致性策略。

## 9. 三层结果设计

系统必须分别保存：

1. `PredictorRawOutput`：复合 Predictor 成功阶段的必要原始数值、图像载荷摘要和阶段结果；不默认保存完整中间张量或激活值。
2. `ToolResult`：复合 Tool 标准化后的平台结果。
3. `NaturalLanguageExplanation`：只基于已保存 ToolResult 生成的解释，单独记录生成状态。

Predictor 失败信息写入 `inference_run` 或阶段错误字段，不伪装成成功的 PredictorRawOutput。

### 9.1 ToolResult 公共字段

```text
status
requested_outputs
data
artifacts
warnings
confidence
input_contract_valid
applicability
out_of_distribution
tool_id
tool_version
model_version
schema_version
provenance
error（失败或部分成功时）
```

### 9.2 SEM 图像结果

生成图像必须出现在 `artifacts` 中并关联 `asset_id`。Artifact 应标明：

- 是否是用户请求的正式输出；
- 是否是力学性能预测所需的中间资产；
- 资产类型和来源类型；
- 图像尺寸、MIME type 和对象引用。

只请求力学性能时，中间 SEM 图像仍出现在 provenance 和 artifact 引用中，但前端可以降低展示优先级。

### 9.3 力学性能结果

当前正式输出为：

```json
{
  "yield_strength": {
    "value": 650.0,
    "unit": "MPa"
  },
  "elongation": {
    "value": 3.2,
    "unit": "%"
  }
}
```

不得返回无单位数值，也不得增加未经模型和人工确认的力学性能字段。

### 9.4 置信度、OOD、输入契约与 applicability

当前模型没有可发布的置信度、不确定度、OOD 算法或正式评价指标。MVP 固定：

```text
confidence = null
out_of_distribution = null
```

不得由 LLM、Tool 或前端编造这些数值。

当前只能依据以下硬规则判断输入契约有效：

- 材料体系为 ZTA35G；
- 四维工艺参数全部位于允许范围。

它们不能证明模型已经完成正式适用性验证。当前仍缺少正式训练数据版本、可发布评价指标和 OOD 验证，因此满足输入硬规则并实际执行时必须返回：

```text
input_contract_valid = true
applicability = limited
warning = 模型尚未完成训练数据版本、评价指标和 OOD 适用性验证；结果仅限当前已确认输入契约范围内使用。
```

不得把 `input_contract_valid=true` 解释为 `applicability=applicable`。不满足材料或参数硬规则时，Application 在 Predictor 调用前拒绝，并以结构化校验错误表达 `input_contract_valid=false`；不能返回预测结果后再标记不适用。

### 9.5 NaturalLanguageExplanation 与 LLM 调用记录

Tool 任务的 `NaturalLanguageExplanation` 只读取已持久化的 `ToolResult`。每次解释生成必须单独记录 LLM 调用来源，至少包括：

```text
llm_provider
llm_model
prompt_template_id
prompt_template_version
generation_parameters
input_result_id
```

这些字段属于 LLM/解释生成记录，不新增 Tool 版本字段，也不复用 `tool_version` 或 `model_version` 表达 LLM 与 Prompt 版本。解释记录还应关联适用的 `ExecutionContext` 标识、生成状态和 LLM 错误摘要，从而区分 Predictor 错误、Tool 标准化错误和 LLM 解释错误。

对于 Tool 任务，`input_result_id` 必须指向实际作为解释输入的 ToolResult。LLM 生成失败不能改变 PredictorRawOutput 或 ToolResult 的成功状态，也不能回写或覆盖结构化结果。

普通知识问答没有 ToolResult，优先保存为 `AssistantMessage` 或单独的知识回答记录，并复用相同的 LLM 调用元数据；此类记录不要求 `input_result_id`。不得为了复用 Tool 解释结构而伪造 result_id。

## 10. 版本、公共 API 与模型 Manifest

MVP 的 Tool/Predictor 结果只保留以下三个版本字段。公共 API 版本和 LLM/Prompt 版本在各自边界单独记录，不混入 Tool 版本语义。

### 10.1 `tool_version`

标识 Tool 实现版本，包括输入校验、参数映射、requested outputs 处理、Predictor 调用、结果组装和错误处理。

初始建议：

```text
tool_version: 0.1.0
```

### 10.2 `model_version`

当前模型包作为整体管理，包含：

- DDPM 图像生成模型；
- DenseNet121 特征提取模型；
- 屈服强度 SVR；
- 延伸率 SVR。

初始建议：

```text
model_version: bundle-v1
```

任何模型权重发生变化时必须创建新的 `model_version`，不得覆盖文件后继续使用原版本。非模型 Tool 的 `model_version` 可以为 `null` 或省略。

### 10.3 `schema_version`

标识 Tool 输入/输出结构的字段、层级、必填项、类型和枚举值。

初始建议：

```text
schema_version: 1.0
```

不兼容的输入或输出结构变化必须更新 `schema_version`。

### 10.4 每次执行的版本记录

```json
{
  "tool_id": "zta35g_sem_virtual_lab",
  "tool_version": "0.1.0",
  "model_version": "bundle-v1",
  "schema_version": "1.0"
}
```

第一阶段不建设完整模型版本管理平台，不管理训练数据版本、模型别名、灰度发布或在线回滚。

### 10.5 公共 API 版本与幂等任务创建

未来公共 HTTP API 使用路径版本，例如：

```text
/api/v1
```

API 路径版本表示公共 HTTP 契约；Tool 的 `schema_version` 表示单个 Tool 输入/输出契约。两者独立演进，不得因为 API 路径为 v1 就假设所有 Tool Schema 也为 1.0，反之亦然。

任务创建接口预留 `idempotency_key`。它属于 API/Application 的任务提交契约，不属于 ZTA35G Tool 输入 Schema。相同主体在有效幂等范围内使用同一 key 重复提交相同请求时，应复用既有 `task_id`，避免创建重复任务；同一 key 对应不同请求内容时应拒绝并返回冲突。具体保存期限和分布式锁实现留到阶段 1 以后决定。

### 10.6 模型包 Manifest

每个可启用模型包必须有 manifest，最少关联：

```text
model_id
model_version
weight_files[].filename
weight_files[].sha256
default_runtime_parameters
compatibility_acceptance_record
runtime_output_encoding
```

当前 `bundle-v1` 的 `weight_files` 将覆盖 DDPM、DenseNet121 和两个 SVR 文件；`default_runtime_parameters` 至少容纳 `num_samples`、`guide_scale` 和 `timesteps`，兼容性验收记录指向第 12 节定义的实际环境和测试结论。`runtime_output_encoding` 至少固定正式 PNG 的颜色模式、预期尺寸、原始值域、映射、裁剪、量化、位深和编码规则。

manifest 是 Predictor Adapter 解析模型包、验证文件完整性和记录运行版本的边界，不替代 Tool Registry，也不把内部权重暴露为用户参数。第一版使用受版本控制的静态 JSON 或等价静态配置，不建设模型注册服务。阶段 0 只定义格式和职责，不计算 SHA-256、不加载或修改权重，也不创建完整模型版本管理平台。

## 11. Task、运行、阶段与解释状态语义

### 11.1 分层状态集合

Task 状态保留并扩展为：

```text
PENDING
RUNNING
NEEDS_INPUT
SUCCEEDED
PARTIALLY_SUCCEEDED
FAILED
CANCELLED
TIMED_OUT
```

Tool Run 和 Inference Run 至少允许：

```text
PENDING
RUNNING
SUCCEEDED
PARTIALLY_SUCCEEDED
FAILED
CANCELLED
TIMED_OUT
```

复合 Predictor 的内部 Stage 至少允许：

```text
PENDING
RUNNING
SUCCEEDED
FAILED
SKIPPED
CANCELLED
TIMED_OUT
```

Task、Tool Run、Inference Run 和 Stage 可以共用部分状态名称，但各自拥有独立状态机和聚合语义。Stage 的成功不直接等于整个 inference 或 task 成功；同样，解释失败也不能反向把真实 Predictor 结果改成失败。`SKIPPED` 表示按请求或前置失败规则未执行，不等于 `FAILED`。

状态示例：

1. 只请求 `sem_image`：`sem_generation=SUCCEEDED`，`mechanical_property_prediction=SKIPPED`，Inference Run 和 Tool Run 为 `SUCCEEDED`。
2. SEM 生成失败：`sem_generation=FAILED`，`mechanical_property_prediction=SKIPPED`，Inference Run、Tool Run 和 Task 为 `FAILED`。
3. 同时请求两项，SEM 成功而性能阶段失败：Inference Run、Tool Run 和 Task 为 `PARTIALLY_SUCCEEDED`。
4. 只请求 `mechanical_properties`，SEM 成功而性能阶段失败：Inference Run 可记录 `PARTIALLY_SUCCEEDED` 以保存阶段事实，但 Tool Run 和 Task 因用户目标未完成而为 `FAILED`。

当前 MVP 不实现用户取消或真实超时控制；`CANCELLED` 和 `TIMED_OUT` 只作为稳定状态协议预留，不表示已存在取消 API、超时中断器或队列控制。

### 11.2 校验、模型和资产失败规则

1. 缺少一个或多个关键参数时，不创建 Tool Run，Task 进入 `NEEDS_INPUT`。
2. 参数类型/精度不合法时，在 Predictor 调用前返回 `INVALID_PROCESS_PARAMETER_PRECISION`。
3. 单位不支持或有歧义时，返回 `INVALID_PROCESS_PARAMETER_UNIT` 或进入 `NEEDS_INPUT`，不得猜测。
4. 工艺参数越界时，在 Predictor 调用前返回 `PROCESS_PARAMETERS_OUT_OF_RANGE`。
5. requested outputs 为空或包含未知枚举值时，由 Application 拒绝；重复的合法值按第 4.1 节去重。
6. 权重或依赖兼容性验收未通过时，真实 Tool 保持不可用并返回 `MODEL_UNAVAILABLE`。
7. 图像生成成功但正式资产无法持久化时，不执行性能阶段；Stage 可以记录生成成功，但 Tool Run 和 Task 为 `FAILED`。
8. 同时请求两项时，图像成功保存但性能阶段失败，保留图像和部分 ToolResult，并按第 11.1 节标记部分成功。
9. Tool、Predictor 或某 Stage 失败后，LLM 不得编造图像、性能数值、置信度、OOD 或替代结果。

### 11.3 Tool 任务的自然语言解释失败

以下链路采用明确的分层状态：

```text
SEM 生成成功
→ 力学性能预测成功
→ ToolResult 保存成功
→ NaturalLanguageExplanation 生成失败

inference_run = SUCCEEDED
tool_run = SUCCEEDED
NaturalLanguageExplanation = FAILED
task = PARTIALLY_SUCCEEDED
```

此时：

- 前端仍可展示正式 SEM 图像和结构化力学性能；
- Task/响应返回明确 warning，说明自然语言解释生成失败；
- 不得因为 LLM 失败而把真实模型结果、Inference Run 或 Tool Run 标记为失败；
- 后续可以只重试自然语言解释，不重新运行模型；
- 每次解释重试继续引用同一个 `input_result_id`；
- 重试创建新的解释尝试或 LLM 调用记录，不覆盖历史失败记录。

### 11.4 普通知识问答任务

每次有效用户提交都可以创建统一 `task_id`。普通知识问答 Task 不创建 Tool Run 或 Inference Run，结果直接保存为 `AssistantMessage` 或等价知识回答记录，并通过 `ExecutionContext`、`trace_id` 和独立 LLM 调用记录实现关联与可观测性。

这样 Tool 任务和非 Tool 任务可以共用对话历史、任务查询和未来 TaskEvent/SSE 边界。知识问答成功时 Task 为 `SUCCEEDED`；如果不存在 ToolResult 且知识回答的 LLM 调用失败，则对应 Task 为 `FAILED`。MVP 不为该路径设计 RAG、知识库检索或复杂回答工作流。

### 11.5 `NEEDS_INPUT` 恢复原则

1. 缺少关键参数时，原 Task 进入 `NEEDS_INPUT`，保留原 `task_id`。
2. 用户在同一会话补充缺失信息后，继续恢复原 `task_id`。
3. 新一次用户补充产生新的 `request_id`。
4. 新的执行尝试产生新的 `trace_id`。
5. 原始用户输入、已识别参数和缺失信息记录不得被覆盖。
6. Application 合并补充信息后重新执行单位标准化、类型、精度、范围和 requested outputs 校验；全部通过后才创建 Tool Run。
7. 用户明确开启无关新任务时创建新的 `task_id`，不能错误复用旧 Task。

具体消息合并、超时关闭和跨会话恢复规则留到第二节或后续任务设计。

## 12. 阶段 1A / 1B 环境隔离与兼容性门槛

### 12.1 当前状态

现有 `D:\ProgramData\Anaconda3\envs\materialsagent` 环境只有 Python 3.13 基础包，尚未安装模型、后端或 LangChain 所需依赖，也未经过模型兼容性验证。

阶段 0 不安装依赖、不运行模型、不修改该 Conda 环境。阶段 1A 和 1B 从一开始使用隔离环境，不预先假定后端与模型依赖最终一定兼容。

### 12.2 第一阶段隔离边界

```text
Windows 宿主机
├─ Conda: materialsagent-backend
│  └─ 阶段 1A 平台基础骨架、Mock Tool / Mock Predictor
├─ Conda: materialsagent-sem-validation
│  └─ 阶段 1B SEM 模型加载与单图链路验证
├─ Node.js 环境
│  └─ Vue 3 + Vite 前端
└─ Docker Desktop / Docker Compose
   ├─ PostgreSQL
   └─ MinIO
```

- 两个 Conda 环境不互相安装对方的专用依赖；
- 前端依赖由 Node.js 和 `package.json` 管理，不进入 Conda；
- PostgreSQL 和 MinIO 由 Docker Compose 管理，不安装到 Conda；
- 不在 WSL 中重复安装另一套 Python、Docker Engine 或项目依赖；
- 不配置 GPU Docker，不将模型训练代码放入容器。

### 12.3 阶段 1A：平台基础骨架环境

候选环境名：

```text
materialsagent-backend
```

基础环境只安装：

```text
FastAPI
Uvicorn
Pydantic
SQLAlchemy
Alembic
PostgreSQL driver
MinIO SDK
configuration management
structured logging
pytest
Mock Tool / Mock Predictor 所需依赖
```

该环境不安装 PyTorch、Torchvision 或旧版 scikit-learn 模型依赖。LangChain 和实际 LLM 提供商包不属于 1A 基础骨架依赖，进入对应接入步骤后再通过受控依赖变更加入。

### 12.4 阶段 1B：SEM 模型验证环境

候选环境名：

```text
materialsagent-sem-validation
```

只安装并验证：

```text
PyTorch
Torchvision
NumPy
SciPy
scikit-learn
Joblib
Pillow
Matplotlib
SEM 模型所需其他必要依赖
```

优先验证 Python 3.10，但最终 Python 和依赖版本由实际权重兼容性测试决定。阶段 1B 环境允许反复调整和重建，不能污染 `materialsagent-backend`。

### 12.5 真实 Predictor 与 Tool 启用前的模型验收

至少验证：

1. `materialsagent-sem-validation` 可以创建和激活；
2. 必要模型依赖能够安装；
3. DDPM、DenseNet 和两个 SVR 文件均能加载；
4. 两个 SVR 可在选定 scikit-learn 环境中读取；
5. DenseNet 旧键名兼容转换有效；
6. DDPM checkpoint 可读取并选择确认的权重分支；
7. 捕获并记录 DDPM `missing_keys` 和 `unexpected_keys`；
8. 影响模型功能的键不匹配为零，或已由模型负责人书面确认；
9. 合法四维工艺参数可生成一张 SEM 图像；
10. 整数温度、最多 1 位小数时间和固定参数顺序可正确传入现有模型链路；
11. 单张图像可以完成 DenseNet 特征提取；
12. 图像特征与四维参数可以正确拼接；
13. 两个 SVR 输出屈服强度和延伸率；
14. 确认图像实际尺寸、原始值域及第 8.1 节 PNG 后处理所需信息；
15. 记录单次完整推理耗时和资源使用；
16. 验证固定 seed 的复现性；
17. 验证模型只加载一次并可被后续请求复用。

以上验收未通过前：

- Tool Registry 中真实工具保持 `enabled=false` 且 `availability=unavailable`；
- 不得开始依赖真实模型行为的 Predictor/Tool 正式封装；
- 不得对外提供 SEM 或性能结果；
- 不得根据静态代码检查宣称模型兼容。

1B 只阻塞真实 Predictor、真实 Tool 启用和真实模型端到端验收；不阻塞阶段 1A 的平台基础骨架、Docker Compose、StorageService、Task、Result、ExecutionContext、Mock Tool 和契约测试。

### 12.6 验证后的环境决策

方案 A：依赖兼容。可以评估合并为：

```text
materialsagent-mvp
```

合并后必须重新执行完整平台测试、模型加载验收和单图端到端验收，不能把两个隔离环境分别通过等同于合并环境已通过。

方案 B：依赖不兼容。继续保持后端与模型环境分离，并通过稳定 Predictor Port 连接：

```text
Predictor Port
→ Remote ZTA35GSEMVirtualLabPredictor Adapter
→ 独立 SEM 推理进程或服务
```

环境分离不应要求重写 Application、MaterialTool、Tool Registry 或 ToolResult。阶段 0 不预先选择 A 或 B，也不实现远程推理服务。

### 12.7 依赖、Manifest 与 `strict=False` 记录

模型验收成功后必须记录实际通过测试的依赖组合，包括 Windows、Python、Conda 环境、PyTorch/Torchvision/CUDA、NumPy/SciPy/scikit-learn/Joblib/Pillow/Matplotlib、GPU/CPU 信息、四个权重哈希和验收结论。记录进入静态 model manifest 或关联的兼容性验收记录；阶段 0 不创建环境文件或锁文件。

阶段 1B 必须捕获 `load_state_dict` 返回的 missing keys 和 unexpected keys，并写入结构化日志和验收记录。不允许因为源码使用 `strict=False` 就静默忽略兼容性问题；存在可能影响模型结构的未匹配权重时，模型包不得标记为可用。

### 12.8 单图与并发边界

MVP 固定 `num_samples=1`，验证单张 SEM 图像生成、单图特征提取、四维参数拼接和两个 SVR 输出。模型应在受控进程启动或懒加载时加载一次并复用，不得每个请求重新加载全部权重。

若生成耗时过长或不能安全并发，MVP 可以限制为单任务同步或进程内执行。阶段 1A 不实现 Redis、Worker、重试中间件或队列拓扑。

### 12.9 阶段 1A / 1B 并行路线

```text
阶段 1A：materialsagent-backend
  Docker Compose（PostgreSQL、MinIO）
  → FastAPI /api/v1 骨架与配置
  → StorageService、Task、Result、Asset 基础边界
  → Sync/InProcessTaskExecutor、InProcessEventPublisher
  → 结构化日志与 ExecutionContext
  → Mock Tool / Mock Predictor 契约路径

阶段 1B：materialsagent-sem-validation
  验证模型依赖与四个权重文件
  → 验证参数精度和单图完整链路
  → 确认图像值域、尺寸和输出编码输入条件
  → 记录 model manifest 哈希与兼容性验收
```

两条路线可以并行。1B 通过后先执行第 12.6 节环境决策，再进入真实 Predictor/Tool 汇合、Tool Registry 启用、真实端到端验收和 LangChain 接入。本节只定义环境和门槛，不表示本轮已经创建或实现任何组件。

## 13. 修订后的最小验收场景

进入阶段 1 后，应在对应任务中逐步把以下 22 个场景转化为契约测试与验收标准，不要求阶段 1A 一次全部完成：

1. 输入有效整数温度和最多 1 位小数时间，只请求 `sem_image`，只生成并保存一张正式 SEM PNG；性能 Stage 为 `SKIPPED`。
2. 输入有效参数，只请求 `mechanical_properties`，内部仍生成并保存中间 SEM PNG，再返回性能结果。
3. 输入有效参数，同时请求两项，只生成一次图像并返回两类结构化结果。
4. 普通知识问题创建 Task 和 AssistantMessage，但不创建 Tool Run 或 Inference Run。
5. 缺少关键参数时 Task 进入 `NEEDS_INPUT`；同会话补充后沿用 `task_id`，使用新的 `request_id` 和 `trace_id` 恢复。
6. 参数类型/精度、单位或范围非法时分别返回对应结构化错误，不调用 Predictor，也不截断、四舍五入或猜测。
7. 合法重复 requested outputs 被确定性去重；空集合、未知枚举、LLM 提议的非法参数或不存在 Tool 由 Application 拒绝。
8. SEM 生成失败时，`sem_generation=FAILED`、性能 Stage 为 `SKIPPED`，Inference Run、Tool Run 和 Task 为 `FAILED`。
9. 同时请求两项，图像成功但性能阶段失败：保留图像，Inference Run、Tool Run 和 Task 为 `PARTIALLY_SUCCEEDED`。
10. 只请求力学性能但性能阶段失败：保留中间图像，Inference Run 可记录部分成功，Tool Run 和 Task 为 `FAILED`。
11. ToolResult 保存成功但 NaturalLanguageExplanation 失败：Inference Run/Tool Run 成功，Task 部分成功；解释可引用同一 `input_result_id` 单独重试。
12. 所有成功性能数值都包含正式单位 MPa 或 %，并保留原始/标准化工艺参数及单位。
13. 所有结果包含 `tool_version`、`model_version` 和 `schema_version`；正式 PNG 编码规则由静态 manifest 和 `model_version` 固定。
14. 所有生成结果记录实际 seed、生成配置、尺寸、MIME type、位深和内容哈希。
15. 权重或依赖兼容性未通过验收时，真实 Tool 不得标记为可用，1A Mock 路径仍可开发。
16. 用户请求上传真实 SEM 图像直接预测时，返回明确的不支持结果，不调用模型。
17. 合法 ZTA35G 输入返回 `input_contract_valid=true`、`applicability=limited` 和明确的模型适用性未完整验证 warning。
18. 同一 Tool Run 首次 Inference Run 失败、第二次成功时，各尝试保留独立状态和错误；最终 asset/result 只指向实际成功尝试。
19. 同一主体以相同 `idempotency_key` 重复提交相同任务时不创建重复 Task；同一 key 配不同输入时返回冲突。
20. 前端资产响应不暴露永久公开 `object_key` URL，只通过受控资产访问边界取得文件。
21. 临时进度和 assistant delta 可以只在进程内发布；任务状态、运行记录、关键审计事件和结果仍持久化。
22. Application Use Case 在 Sync/InProcessTaskExecutor 和测试替代 Executor 下保持相同业务规则，不直接依赖 Redis 或 Worker。

验收责任分配：

| 后续任务 | 主要覆盖内容 |
|---|---|
| 阶段 1A 平台骨架 | Task/状态存储、ExecutionContext、幂等、StorageService、进程内 Event、Mock Tool、场景 4–7、19–22 的基础契约 |
| 阶段 1B 模型验证 | 权重兼容性、参数精度传递、单图尺寸和值域和 manifest 输入条件；只提供模型前置证据，不负责 Tool、LLM 或前端验收 |
| 真实 Tool 汇合 | 正式 Predictor、ToolResult、PNG 资产、失败/部分成功和溯源场景 |
| LangChain 接入 | 自然语言参数/单位提取、知识回答、追问和解释失败/重试场景 |
| 后续 SSE | 基于 TaskEventStream 的流式投递，不改变场景 21 的持久化边界 |
| 后续用户系统 | 将本地 actor 边界扩展为用户、项目和租户访问控制 |

## 14. 当前简单实现—稳定接口—未来替换实现

| 当前简单实现 | 稳定接口/契约 | 未来替换实现 | 替换时不应改动 |
|---|---|---|---|
| Local Predictor | Predictor Port | Remote Predictor / 独立推理服务 | Application、MaterialTool 公共契约、ToolResult |
| InProcess Task / Sync Task | TaskExecutor Port | QueueTaskExecutor、Redis、Worker | Application Use Case 业务规则、Task 状态协议 |
| InProcess Event | EventPublisher | Redis Publisher / Event Bus | Event 发布调用方、TaskEvent 公共字段 |
| 普通非流式响应 | TaskEvent Schema、未来 TaskEventStream | SSE Adapter | 任务/结果业务模型、EventPublisher |
| 结构化日志 | Observability / Telemetry Port | OpenTelemetry、LangSmith、Metrics/Alerting | 业务 Event 协议、Application 业务规则 |
| 代码级静态 Tool Registry | Registry Contract | 后续动态管理或受控插件机制 | MaterialTool 契约、Tool Catalog 投影语义 |
| 本地匿名用户 | ActorContext | 正式用户、项目权限和多租户 | Task、Asset、Result 的所有权关联边界 |

该表只定义替换点和依赖方向。本阶段不创建远程服务、队列、Redis、SSE、OpenTelemetry、动态插件系统或正式用户系统。

## 15. 第二节待明确事项

第一节只预留边界，以下执行流程和组装细节留到第二节明确：

1. MinIO 文件上传和 PostgreSQL 资产登记的执行顺序；
2. 任一步失败时的补偿策略；
3. orphan object / orphan metadata 的识别和处理；
4. `NEEDS_INPUT` 恢复的数据流、消息合并和关闭条件；
5. 普通知识问答的任务、LLM 调用、消息保存和响应数据流；
6. 正式 PNG 的编码、位深、`[-1, 1]` 值域映射、裁剪和量化规则；
7. Task、Tool Run、Inference Run 和 Stage 的状态迁移图；
8. ToolResult、资产、NaturalLanguageExplanation 和前端响应的组装过程。

本轮不设计具体数据库表、事务算法、补偿实现、消息接口或图像编码代码。

## 16. 设计一致性与旧术语检查

本文使用且仅使用以下 MVP Tool、Predictor Port 和条件 Adapter 名称：

```text
tool_id: zta35g_sem_virtual_lab
Tool: ZTA35GSEMVirtualLabTool
Predictor Port: ZTA35GSEMVirtualLabPredictor
方案 A Local Adapter: LocalZTA35GSEMVirtualLabPredictor
方案 B Remote Adapter: RemoteZTA35GSEMVirtualLabPredictor
```

一致性规则：

- 一个模型任务只有一个复合 tool_run；
- Local/Remote 只是同一 Predictor Port 的环境适配选择，不产生第二个 Tool 或 Predictor Port；
- 一次实际复合 Predictor 调用对应一个 inference_run；一个逻辑 tool_run 重试时可以有多个 inference run，且各自保留状态、错误、资产和结果来源；
- `mechanical_properties` 永远依赖内部先生成单张 SEM 图像；
- 用户上传真实 SEM 图像不属于 MVP；
- 内部生成图像必须登记资产，即使前端不主要展示；
- Tool Registry 是唯一工具事实来源，Tool Catalog 是只读投影；
- 所有 Tool 实现最小 MaterialTool 契约，但 Tool 不必依赖 Predictor；
- LangChain 不执行函数、不选择具体版本、不控制内部阶段；
- StorageService 是唯一文件访问边界；
- `object_key` 是内部引用，不是永久公开 URL；
- ActorContext 与 ExecutionContext 只预留最小传递和关联边界，不表示已实现认证或多租户；
- Event 表示业务变化，Observability 表示 logs/traces/metrics，EventPublisher 不是完整可观测性实现；
- Application Use Case 依赖 TaskExecutor Port，不依赖同步、队列或 Worker 细节；
- 温度为整数、时间最多 1 位小数；单位和 requested outputs 由 Application 确定性规范化，LLM 不做最终换算；
- Task、Tool Run、Inference Run、Stage 和解释记录的状态分层聚合，`SKIPPED` 不等于失败；
- Tool 任务解释失败不覆盖模型结果；普通知识问答不伪造 ToolResult；
- `NEEDS_INPUT` 恢复原 task，但产生新的 request 和 trace，校验通过后才创建 Tool Run；
- 公共 API 版本、Tool `schema_version`、模型版本和 LLM/Prompt 版本各自独立；
- 合法输入只证明 `input_contract_valid=true`，当前模型 `applicability=limited`；
- 正式 SEM 资产为单张、无可视化装饰的灰度 PNG 契约，后处理由静态 model manifest 和 `model_version` 固定；
- 阶段 1A 使用 `materialsagent-backend`，阶段 1B 使用 `materialsagent-sem-validation`；仅在验证兼容后评估 `materialsagent-mvp`，不预设必然合并；
- 模型和权重兼容性验证只阻塞真实 Predictor/Tool 启用，阶段 1A 平台骨架与阶段 1B 模型验证可以并行；
- Observability、ActorContext、TaskExecutor、EventPublisher 和 model manifest 均保持 MVP 最小实现，不扩展为完整平台；
- 不新增通用 Workflow Engine、插件市场、复杂权限系统或完整模型管理平台；
- 当前仍停留在阶段 0，未进入阶段 1 或实现阶段。
