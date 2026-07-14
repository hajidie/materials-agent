# 材料智能体平台阶段 0：ZTA35G SEM Virtual Lab MVP 范围与总体架构

> 状态：第一节修订稿，等待人工审阅
>
> 修订日期：2026-07-14
>
> 适用范围：阶段 0 文档设计
>
> 事实依据：对 `SEM/` 目录进行静态、只读检查，并结合项目负责人于 2026-07-14 确认的模型业务契约；未导入模型、未加载权重、未执行训练或推理。

## 1. 本次修订结论

第一阶段将 SEM 图像生成与力学性能预测设计成同一个复合能力。当前源码本身是一个耦合的虚拟实验室链路，性能预测依赖四维工艺参数和模型内部生成的 SEM 图像特征。因此 MVP 只注册一个材料范围明确的复合 Tool，并通过一个复合 Predictor Port 调用本地模型组合：

```text
tool_id: zta35g_sem_virtual_lab
Tool: ZTA35GSEMVirtualLabTool
Predictor Port: ZTA35GSEMVirtualLabPredictor
Local Adapter: LocalZTA35GSEMVirtualLabPredictor
```

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

| 代码参数 | Tool 输入字段 | 正式业务名称 | 单位 | 允许范围 |
|---|---|---|---|---|
| `T1` | `solution_temperature` | 固溶温度 | °C | 900–1100 |
| `t1` | `solution_time` | 固溶时间 | h | 1–5 |
| `T2` | `aging_temperature` | 时效温度 | °C | 670–790 |
| `t2` | `aging_time` | 时效时间 | h | 1–5 |

平台不得把业务字段名称直接暴露为含义不明确的 `T1/t1/T2/t2`，但 Predictor Adapter 可以在边界内把正式业务字段映射为源码所需顺序。

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
- 独立模型服务；
- GPU 容器、GPU 集群和自动扩缩容；
- 完整可观测平台和完整评价体系；
- 用户、权限、计费和多租户；
- 模型训练和数据标注平台；
- 动态插件安装或数据库动态注册；
- 完整模型版本管理平台、训练数据版本、模型别名、灰度发布和在线回滚；
- 生产级容器部署。

“不进行生产级容器部署”不等于完全不使用 Docker。阶段 1 计划仅通过 Docker Compose 管理 PostgreSQL 和 MinIO；FastAPI、LangChain 和本地复合 Predictor 暂时运行在 Windows 宿主机。

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

不得接受未知枚举值。数组顺序不决定执行顺序；重复值使用 `INVALID_REQUESTED_OUTPUT` 拒绝，不做静默去重。

ZTA35G 是该 Tool 的固定适用材料，不是可自由替换的模型参数。自然语言任务明确涉及其他材料时，Application 必须拒绝并返回 `UNSUPPORTED_MATERIAL`；材料意图不明确且可能影响工具选择时，LangChain 应追问，不能自动泛化该工具。

### 4.2 参数校验

Application 和 Tool 必须在 Predictor 调用前执行确定性校验：

- 四个工艺参数全部必填，不得由 LLM 或代码静默补全；
- 固溶温度必须位于 900–1100 °C；
- 固溶时间必须位于 1–5 h；
- 时效温度必须位于 670–790 °C；
- 时效时间必须位于 1–5 h；
- 超出范围必须拒绝，不采用“警告后继续推理”；
- 不得静默改变单位、交换字段顺序或把缺失值替换为默认值。

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

### 4.3 平台模型配置

以下内容属于平台配置，不直接作为普通用户业务参数：

- `num_samples`：MVP 固定为 1；
- `guide_scale`：MVP 平台固定为当前代码默认值 2.0；
- `timesteps`：MVP 固定为当前模型配置 1000；
- seed 策略：MVP 由平台生成一个可记录的 32 位无符号整数并传给 Predictor；
- 模型权重路径或模型包定位信息；
- 设备和并发策略。

普通用户不能覆盖 `num_samples`、`guide_scale`、`timesteps`、权重路径或设备。以后若为高级用户开放 seed，应通过兼容的 Schema 变更显式加入。

### 4.4 实际推理运行参数

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

复合 Predictor 不依赖 PostgreSQL、MinIO、StorageService、Tool Registry、LangChain 或 HTTP。Local Adapter 应返回阶段化的复合结果，至少能够表达：

- `sem_generation` 是否成功；
- 成功时的单张图像载荷或可由 Tool 持久化的中立表示；
- `mechanical_property_prediction` 是否执行及是否成功；
- 成功时的屈服强度和延伸率原始值；
- 失败阶段、错误类别和可安全记录的信息；
- 实际运行参数。

如果图像生成成功但性能阶段发生可捕获异常，Local Adapter 必须把已生成图像随阶段化失败结果返回，使 ToolExecutionService 能通过 AssetService/StorageService 保存它。不得把 StorageService 注入模型代码来换取中间资产保存。

MVP 对进程被强制终止、宿主机崩溃或显存进程直接退出时的中间资产可靠保存不作保证；可靠阶段检查点属于后续队列和独立推理服务演进范围。

## 6. 模块化单体总体架构

```text
Vue 3 + Vite Frontend
   ↓
FastAPI API
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
Core Ports / Registries
   ├─ Tool Registry
   │    └─ ZTA35GSEMVirtualLabTool
   │          └─ ZTA35GSEMVirtualLabPredictor Port
   ├─ StorageService Port
   ├─ Repository Ports
   └─ EventPublisher Port
          ↓
Infrastructure Adapters
   ├─ PostgreSQL Repositories
   ├─ MinIO StorageService
   ├─ LocalZTA35GSEMVirtualLabPredictor
   └─ InProcessEventPublisher
```

### 6.1 Application 不是巨型类

- `ChatOrchestrationService`：取得 LangChain 的结构化意图，决定知识回答还是允许的 Application Use Case。
- `RunZTA35GSEMVirtualLabUseCase`：协调一次复合 Tool 用例，不实现模型细节。
- `ToolExecutionService`：解析 Tool、创建运行记录、调用 Tool 并处理阶段化结果。
- `AssetService`：通过 StorageService 保存和登记生成资产。
- `TaskService`：创建任务并执行状态迁移。
- `ResultService`：保存 PredictorRawOutput 摘要、ToolResult 和自然语言解释。

底层服务不得反向依赖 `ChatOrchestrationService`。复合模型内部阶段不应扩展成通用 WorkflowOrchestrationService。

### 6.2 LangChain 与 Application 边界

LangChain 只负责：

- 理解是否需要调用 `zta35g_sem_virtual_lab`；
- 从自然语言提取四维工艺参数建议；
- 提议 `requested_outputs`；
- 识别明显缺失的输入或歧义；
- 必要时生成追问。

LangChain 只返回稳定 `tool_id` 和结构化参数建议，不选择 `tool_version`、`model_version`，不直接执行函数，也不得绕过 Application 构造任意内部阶段调用。

Application 负责：

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

Tool Catalog 是从 Registry 元数据生成的只读 LangChain 投影，不单独维护或持久化。Catalog 中必须明确：

- 只适用于 ZTA35G 钛合金；
- 必需四维工艺参数；
- 支持的 requested outputs；
- 不接受用户上传 SEM 图像；
- 工艺范围和主要错误类型。

### 6.4 文件与数据库边界

所有文件必须通过 StorageService 访问：

- MinIO 保存生成 SEM 图像和其他结果文件本体；
- PostgreSQL 保存资产元数据、对象键、任务、运行、结果、错误、版本和溯源关系；
- Predictor 不接触 MinIO、本地平台目录或数据库；
- 业务层不得直接使用 MinIO SDK；
- 不得把宿主机绝对路径保存为资产引用。

### 6.5 Event 边界

Event 模块定义版本化事件结构和 `EventPublisher` Port。MVP 使用 `InProcessEventPublisher`，不承诺可靠投递。

复合运行仍应产生阶段级事件，例如：

```text
tool_run_started
sem_generation_started
sem_generation_succeeded / sem_generation_failed
mechanical_property_prediction_started
mechanical_property_prediction_succeeded / mechanical_property_prediction_failed
tool_run_succeeded / tool_run_partially_succeeded / tool_run_failed
```

后续可以替换为 Redis 发布器，并由 SSE 订阅层推送状态；Application 不依赖 Redis 或 SSE。

## 7. 标识、运行关系与阶段可观测性

### 7.1 标识定义

- `request_id`：一次 HTTP 请求。
- `conversation_id`：一组关联的多轮交互。
- `task_id`：稳定业务任务。
- `trace_id`：一次实际执行链路。
- `tool_run_id`：任务中一次 `zta35g_sem_virtual_lab` 逻辑执行。
- `inference_run_id`：一次复合 Predictor 调用。
- `asset_id`：上传、生成或派生文件资产；本 MVP 只使用模型生成 SEM 资产。
- `result_id`：一个持久化结果聚合。

一次模型任务形成：

```text
task_id / trace_id
└─ tool_run_id: zta35g_sem_virtual_lab
   └─ inference_run_id: composite inference
      ├─ stage: sem_generation
      │  └─ generated sem asset_id
      └─ stage: mechanical_property_prediction（按 requested_outputs 决定是否执行）
         └─ mechanical-property result_id
```

一个逻辑 `tool_run_id` 可以因整体重试关联多个 `inference_run_id`。MVP 不把内部阶段登记成 Tool，也不为各阶段分别创建 `tool_run_id`。

### 7.2 阶段记录

复合 `inference_run` 必须记录内部阶段状态，但无需在阶段 0 决定具体数据库表结构。每个阶段至少预留：

- 阶段名；
- 开始和结束时间；
- 状态；
- 错误码和安全错误摘要；
- 关联 `asset_id` 或结果引用；
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
- `task_id`；
- `tool_run_id`；
- `inference_run_id`；
- 四维工艺参数及单位；
- 实际 seed；
- 实际 guide scale；
- 实际 timesteps；
- 图像尺寸和 MIME type；
- `model_id`；
- `model_version`；
- `tool_version`；
- `object_key`；
- 内容哈希或校验值；
- `created_at`。

MVP 固定 `num_samples=1`。不得把多图拼图作为正式模型结果。若以后额外生成可视化拼图，它只能是 `asset_type: visualization`、`source_type: derived` 的派生资产，不能替代原始单张 SEM 资产。

### 8.2 力学性能结果溯源

性能结果必须关联：

- 内部生成 SEM 图像的 `asset_id`；
- 四维工艺参数及单位；
- `tool_id`、`tool_version`；
- `model_version`；
- `schema_version`；
- `task_id`、`trace_id`、`tool_run_id`、`inference_run_id`、`result_id`。

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

### 9.4 置信度、OOD 与 applicability

当前模型没有可发布的置信度、不确定度、OOD 算法或正式评价指标。MVP 固定：

```text
confidence = null
out_of_distribution = null
```

不得由 LLM、Tool 或前端编造这些数值。

`applicability` 只依据已确认硬规则：

- 材料体系为 ZTA35G；
- 四维工艺参数全部位于允许范围。

满足条件并实际执行时：

```text
applicability: applicable
```

不满足条件时在 Predictor 调用前拒绝，不能返回预测结果后再标记不适用。

## 10. 简化版本设计

MVP 只保留三个版本字段：

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

## 11. 任务状态、失败与部分成功

任务和 Tool 状态至少预留：

```text
PENDING
RUNNING
NEEDS_INPUT
SUCCEEDED
PARTIALLY_SUCCEEDED
FAILED
```

规则：

1. 缺少一个或多个工艺参数时，不调用 Tool，任务进入 `NEEDS_INPUT`。
2. 工艺参数越界时，在 Predictor 调用前拒绝，返回 `PROCESS_PARAMETERS_OUT_OF_RANGE`。
3. requested outputs 为空或包含非法值时，由 Application 拒绝执行。
4. 权重或依赖兼容性验收未通过时，Tool 必须保持不可用，返回 `MODEL_UNAVAILABLE`。
5. `sem_generation` 失败时，不执行性能阶段，tool_run 和 task 为 `FAILED`。
6. 图像生成成功但资产无法持久化时，不执行性能阶段；inference stage 可以记录生成成功，但 tool_run 和 task 为 `FAILED`。
7. 同时请求两项时，图像生成并保存成功、性能阶段失败：
   - 生成图像资产保留；
   - ToolResult 包含成功图像 artifact；
   - tool_run 和 task 为 `PARTIALLY_SUCCEEDED`；
   - 错误明确指出 `mechanical_property_prediction` 阶段失败；
   - LLM 只解释生成图像和失败事实，不得编造性能值。
8. 只请求 `mechanical_properties` 时，若图像生成成功但性能阶段失败，中间图像仍保留；由于用户请求的业务结果未完成，tool_run 和 task 为 `FAILED`，不是 `PARTIALLY_SUCCEEDED`。
9. 只请求 `sem_image` 时不执行性能阶段，图像成功保存后即为 `SUCCEEDED`。
10. Tool、Predictor 或某阶段失败后，LLM 不得编造图像、性能数值、置信度、OOD 或替代结果。

## 12. 环境、依赖与兼容性门槛

### 12.1 当前状态

现有 `D:\ProgramData\Anaconda3\envs\materialsagent` 环境只有 Python 3.13 基础包，尚未安装模型、后端或 LangChain 所需依赖，也未经过模型兼容性验证。

阶段 0 不安装依赖、不运行模型、不修改该 Conda 环境。环境创建、依赖安装和模型验证是进入阶段 1 前的阻塞性验收任务。

### 12.2 第一阶段运行边界

```text
Windows 宿主机
├─ 项目专用 Conda 环境
│  ├─ FastAPI 后端
│  ├─ LangChain
│  ├─ LocalZTA35GSEMVirtualLabPredictor
│  └─ Python 测试与开发工具
├─ Node.js 环境
│  └─ Vue 3 + Vite 前端
└─ Docker Desktop / Docker Compose
   ├─ PostgreSQL
   └─ MinIO
```

- Python 后端和本地复合 Predictor 暂时共用一个项目专用 Conda 环境；
- 前端依赖由 Node.js 和 `package.json` 管理，不进入 Conda；
- PostgreSQL 和 MinIO 由 Docker Compose 管理，不安装到 Conda；
- 不在 WSL 中重复安装另一套 Python、Docker Engine 或项目依赖；
- 不配置 GPU Docker，不将模型训练代码放入容器。

若以后出现 Python、PyTorch、CUDA 或 scikit-learn 冲突，再将后端和模型推理拆为不同环境或独立推理服务。

### 12.3 项目专用环境候选

进入阶段 1 前新建项目专用 Conda 环境，不继续向当前 Python 3.13 环境无计划安装依赖。

候选环境名：

```text
materialsagent-mvp
```

优先验证 Python 3.10，但最终版本必须由实际权重和依赖兼容性测试决定，不能只按“最新版”选择。

### 12.4 分阶段安装依赖

模型兼容性验证阶段先只安装模型必需依赖：

```text
torch
torchvision
numpy
scipy
scikit-learn
joblib
Pillow
matplotlib
```

具体版本通过模型加载和单图完整推理验证确定，不默认全部使用最新版本。

模型验证通过后，后端骨架阶段再加入：

```text
FastAPI
Uvicorn
Pydantic
SQLAlchemy
Alembic
PostgreSQL driver
MinIO Python SDK
configuration management
structured logging
pytest
```

只有进入 LangChain 接入阶段时，才加入 LangChain、实际 LLM 提供商适配包和必要 HTTP 客户端。不提前安装未使用的集成包。

前端阶段再安装 Node.js LTS，由 `package.json` 和锁文件管理依赖。

### 12.5 模型环境阻塞性验收

正式 Tool 封装前至少验证：

1. 项目专用 Conda 环境可以创建和激活；
2. 必要 Python 依赖能够安装；
3. DDPM、DenseNet 和两个 SVR 文件均能加载；
4. 两个 SVR 可在选定 scikit-learn 环境中读取；
5. DenseNet 旧键名兼容转换有效；
6. DDPM checkpoint 可读取并选择确认的权重分支；
7. 捕获并记录 DDPM `missing_keys` 和 `unexpected_keys`；
8. 影响模型功能的键不匹配为零，或已由模型负责人书面确认；
9. 合法四维工艺参数可生成一张 SEM 图像；
10. 单张图像可以完成 DenseNet 特征提取；
11. 图像特征与四维参数可以正确拼接；
12. 两个 SVR 输出屈服强度和延伸率；
13. 记录单次完整推理耗时和资源使用；
14. 验证固定 seed 的复现性；
15. 验证模型只加载一次并可被后续请求复用。

以上验收未通过前：

- Tool Registry 不得把工具标记为可用；
- 不得开始正式 Tool 封装；
- 不得对外提供 SEM 或性能结果；
- 不得根据静态代码检查宣称模型兼容。

### 12.6 依赖记录与锁定

模型验收成功后必须记录实际通过测试的依赖组合，不得只记录宽泛版本范围。记录至少包括：

- Windows 版本、Python 版本和 Conda 环境名；
- PyTorch、Torchvision、CUDA runtime 和 NVIDIA 驱动版本；
- NumPy、SciPy、scikit-learn、Joblib、Pillow 和 Matplotlib 版本；
- 四个模型文件的内容哈希；
- CPU/GPU 设备信息；
- 可复现的环境声明和精确依赖锁定结果；
- 对应模型兼容性验收记录。

阶段 0 只规定记录要求，不创建环境文件或锁文件。

### 12.7 `strict=False` 专项规则

阶段 1 模型验证必须捕获 `load_state_dict` 返回的 missing keys 和 unexpected keys，并写入结构化日志和验收记录。

不允许因为源码使用 `strict=False` 就静默忽略兼容性问题。存在可能影响模型结构的未匹配权重时，模型包不得标记为可用。

### 12.8 单图和并发策略

MVP 固定：

```text
num_samples = 1
```

必须验证：

```text
单张 SEM 图像生成
→ 单图特征提取
→ 与四维工艺参数拼接
→ 两个 SVR 输出
```

复合模型应在进程启动或受控懒加载时加载一次并复用，不得每个请求重新加载全部权重。

若生成耗时过长或不能安全并发，MVP 可以限制为单任务执行。Redis、Worker、队列和独立推理服务留到后续阶段。

### 12.9 后续环境拆分条件

出现以下任一情况时评估拆分：

- 新工具需要不同 Python 版本；
- 工具间需要不兼容的 PyTorch 或 CUDA；
- SEM 模型依赖旧 scikit-learn，而后端或其他工具需要新版本；
- 一个模型的依赖升级会破坏其他工具。

演进路径：

```text
Backend Environment
→ Remote ZTA35GSEMVirtualLabPredictor Adapter
→ Independent SEM Inference Environment or Service
```

Tool 只依赖 Predictor Port，因此环境拆分不应要求重写 Application、Tool Registry 或 ToolResult。

### 12.10 阶段顺序

```text
阶段 0 文档确认
→ 创建项目专用 Conda 环境
→ 验证依赖与四个权重文件
→ 验证单图完整链路
→ 固定并记录可运行依赖组合
→ 开始 FastAPI 后端骨架
→ 按阶段增加后端依赖
→ 接入复合 Predictor 和复合 Tool
→ 最后接入 LangChain
```

## 13. 修订后的最小验收场景

阶段 1 开始前，应把以下场景转化为契约测试与验收标准：

1. 输入有效四维工艺参数，只请求 `sem_image`，生成并保存一张 SEM 图像。
2. 输入有效四维工艺参数，只请求 `mechanical_properties`，内部仍生成并保存中间 SEM 图像，再返回性能结果。
3. 输入有效四维工艺参数，同时请求两项，只生成一次图像并返回两类结果。
4. 普通知识问题，不创建模型 tool_run 或 inference_run。
5. 缺少一个或多个工艺参数，任务进入 `NEEDS_INPUT`。
6. 任一工艺参数越界，返回 `PROCESS_PARAMETERS_OUT_OF_RANGE`，不调用 Predictor。
7. LLM 提议非法参数、未知 requested output 或不存在 Tool 时，由 Application 拒绝。
8. SEM 图像生成失败，不执行性能阶段。
9. 同时请求两项，图像生成成功但性能阶段失败：保留图像并标记 `PARTIALLY_SUCCEEDED`。
10. 所有成功性能数值都包含正式单位 MPa 或 %。
11. 所有结果包含 `tool_version`、`model_version` 和 `schema_version`。
12. 所有生成结果记录实际 seed 和生成配置。
13. 权重或依赖兼容性未通过验收时，Tool 不得标记为可用。
14. 用户请求上传真实 SEM 图像直接预测时，返回明确的不支持结果，不调用模型。
15. 只请求力学性能但性能阶段失败时，保留中间图像，任务整体为 `FAILED`，不误标为部分成功。

## 14. 设计一致性与旧术语检查

本文使用且仅使用以下 MVP Tool 和 Predictor 名称：

```text
tool_id: zta35g_sem_virtual_lab
Tool: ZTA35GSEMVirtualLabTool
Predictor Port: ZTA35GSEMVirtualLabPredictor
Local Adapter: LocalZTA35GSEMVirtualLabPredictor
```

一致性规则：

- 一个模型任务只有一个复合 tool_run；
- 一次复合 Predictor 调用对应一个 inference_run，并保留内部阶段状态；
- `mechanical_properties` 永远依赖内部先生成单张 SEM 图像；
- 用户上传真实 SEM 图像不属于 MVP；
- 内部生成图像必须登记资产，即使前端不主要展示；
- Tool Registry 是唯一工具事实来源，Tool Catalog 是只读投影；
- LangChain 不执行函数、不选择具体版本、不控制内部阶段；
- StorageService 是唯一文件访问边界；
- 环境和权重兼容性验证是阶段 1 前的阻塞门槛；
- 当前不进入第二节或实现阶段。
