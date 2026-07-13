# 材料智能体平台阶段 0：SEM MVP 范围与总体架构

> 状态：第一节修订稿
>
> 检查日期：2026-07-13
>
> 适用范围：阶段 0 文档设计
>
> 事实依据：对 `SEM/` 目录进行静态、只读检查；未导入模型、未加载权重、未执行训练或推理。

## 1. SEM 原始目录只读检查摘要

### 1.1 文件与目录

```text
SEM/
├─ initial_para.csv
├─ train_images/
│  └─ 50 个 512×512 PNG 图像，文件名包含工艺参数与裁剪编号
└─ ZTA35G_lab/
   ├─ virtual_lab_sem.py
   ├─ Update_SVR_model.py
   ├─ ddpm_512_epoch_800.pth
   ├─ densenet121-a639ec97.pth
   └─ SVR_model/
      ├─ Final_Yield_Strength.pkl
      └─ Final_Elongation.pkl
```

文件类型统计：1 个 CSV、50 个 PNG、2 个 Python 文件、2 个 PTH 权重、2 个 PKL 模型。DDPM 权重约 1.86 GiB，DenseNet 权重约 30.85 MiB。

### 1.2 代码与数据分类

| 类别 | 当前内容 | 结论 |
|---|---|---|
| 训练代码 | 未发现训练循环、优化器、损失计算或模型 `fit` 流程 | 当前仓库不包含可确认的训练代码 |
| 模型结构 | `virtual_lab_sem.py` 中的条件 DDPM U-Net、条件嵌入、残差块、自注意力及上下采样模块 | 已确认 |
| 推理代码 | `VirtualLab.generate_images`、`extract_features`、`predict_performance`、`run` 和命令行入口 | 已确认，但目前耦合在同一文件和同一 `VirtualLab` 实例中 |
| 模型迁移脚本 | `Update_SVR_model.py` 只加载并覆盖保存两个 Joblib 模型 | 不是训练代码；会改写权重，阶段 0 不运行 |
| 数据 | `initial_para.csv` 含 25 行四维工艺参数及屈服强度、延伸率；`train_images/` 含 5 组参数、每组 10 张裁剪图 | 文件用途看似训练或参考数据，但缺少训练脚本，确切用途待人工确认 |

### 1.3 权重加载方式

| 模型 | 权重文件 | 加载方式 | 已识别约束 |
|---|---|---|---|
| 条件 DDPM U-Net | `ddpm_512_epoch_800.pth` | `torch.load(..., map_location=device)`；优先取 `ema`，其次 `model`，否则使用整个 checkpoint；移除 `module.`/`model.` 前缀；`strict=False` | `strict=False` 可能掩盖缺失或多余权重键，平台化前必须校验加载报告 |
| DenseNet121 特征提取器 | `densenet121-a639ec97.pth` | 构造 `torchvision.models.densenet121(weights=None)`；CPU 加载；转换旧版 denselayer 键名；严格加载 | 权重与 torchvision 版本兼容范围未记录 |
| 屈服强度 SVR | `Final_Yield_Strength.pkl` | `joblib.load` | 序列化元数据显示 scikit-learn 1.0.2；包含预处理/PCA/SVR 管道信息 |
| 延伸率 SVR | `Final_Elongation.pkl` | `joblib.load` | 序列化元数据显示 scikit-learn 1.0.2；包含预处理/SVR 管道信息 |

### 1.4 已确认的生成模型契约

当前代码中的真实输入为四维工艺参数，固定顺序如下：

| 字段 | 含义 | 单位 | 代码中的训练范围 | 命令行精度处理 |
|---|---|---|---|---|
| `T1` | 第一级温度 | °C | 900–1100 | 取整数 |
| `t1` | 第一级时间 | h | 1–5 | 保留 1 位小数 |
| `T2` | 第二级温度 | °C | 670–790 | 取整数 |
| `t2` | 第二级时间 | h | 1–5 | 保留 1 位小数 |

生成预处理与推理行为：

- 按硬编码 `TRAIN_MIN`/`TRAIN_MAX` 将四维参数归一化；超出范围时命令行只警告，不拦截。
- 默认 `num_samples=3`、`guide_scale=2.0`、`TIMESTEPS=1000`。
- 使用工艺参数元组的 Python `hash` 计算种子，再调用 `torch.manual_seed`。
- 从标准正态噪声开始执行条件 DDPM 反向扩散。
- 原始返回为 `[num_samples, 1, 512, 512]` 的 `torch.Tensor`，值域 `[-1, 1]`。
- `save_visualization` 将多张样本画入一张带工艺参数标题的 PNG 组合图；当前代码没有保存每张原始生成样本为独立文件。
- 当前返回值没有显式返回随机种子、CFG 参数、预处理版本或后处理版本。

因此，平台 ToolResult 可预留这些字段，但不能声称当前 Predictor 已提供完整溯源元数据。是否把每个生成样本分别登记为资产，还是只登记组合图和原始样本清单，待阶段 1 契约测试前人工确认。

### 1.5 已确认的性能预测模型契约

当前性能预测并不是“只输入一张 SEM 图像”。代码中的真实链路是：

```text
四维工艺参数 [T1, t1, T2, t2]
    +
生成 SEM 张量 [B, 1, H, W]，值域 [-1, 1]
    ↓
转为 [0, 1] → 复制为 3 通道 → ImageNet Normalize
    ↓
DenseNet121 features → ReLU
    ↓
GAP + GMP + GMinP，并跨 B 个样本分别取均值
    ↓
图像特征向量与四维原始工艺参数拼接
    ↓
两个 SVR 管道
    ↓
屈服强度（MPa）与延伸率（%）
```

已确认输出只有：

- `yield_strength`：浮点数，代码显示单位为 MPa；
- `elongation`：浮点数，代码显示单位为 %。

当前代码没有输出置信度、不确定度、适用性判断或 OOD 结果，也没有把这些字段写入模型文件之外的版本化 Schema。

### 1.6 独立运行能力与真实 SEM 输入

静态检查结论：

- `generate_images`、`extract_features`、`predict_performance` 是分开的公开方法，从函数边界看可以被未来的两个 Predictor 适配器分别调用。
- 但 `VirtualLab` 初始化时会同时加载 DDPM、DenseNet 和两个 SVR。因此按当前类直接使用时，两个能力并未实现资源和生命周期上的独立加载。
- 当前唯一完整入口 `run` 固定执行“生成→特征提取→预测→保存”，没有“只生成”或“读取上传图片后只预测”的平台入口。
- `extract_features` 接受已在内存中的单通道张量，代码没有实现图片文件解码、灰度转换、尺寸校验/缩放、位深处理或真实 SEM 图像标准化。
- `predict_performance` 明确还需要四维工艺参数，不能据此定义为“仅凭 SEM 图片预测”。
- 注释明确把 `extract_features` 的输入描述为生成 SEM 图像。现有代码和数据不足以证明用户上传的真实 SEM 图像与生成图像同域，或证明 SVR 对真实图像有效。

因此：

- “生成模型是否可独立使用”在算法方法级已确认，在当前运行封装级未实现独立加载。
- “性能模型是否可独立使用”仅在调用者同时提供四维工艺参数和符合约束的张量时具备方法级边界；当前没有独立文件输入流程。
- “性能预测是否支持用户上传的真实 SEM 图像”标记为**待人工确认**，在得到数据来源、训练域和验证指标前不得声明支持。

### 1.7 路径与环境依赖

- 主脚本没有硬编码 Windows/Linux 绝对路径；权重和输出目录通过脚本所在目录计算。
- `OUTPUT_DIR` 仍然直接指向本地 `Generated_Results`，不符合未来“所有文件经 StorageService”的平台约束。
- `Update_SVR_model.py` 使用依赖当前工作目录的相对路径，并会覆盖 PKL 文件。
- 工艺范围、图像尺寸、扩散步数和默认采样配置均硬编码在脚本中。
- 代码依赖 Python、NumPy、Joblib、Matplotlib、PyTorch、Torchvision；加载 SVR 还隐含依赖 scikit-learn 及其兼容的 NumPy/SciPy。
- 仓库没有 requirements、conda environment、PyProject、CUDA 版本或锁文件。
- 指定环境 `D:\ProgramData\Anaconda3\envs\materialsagent` 当前可确认 Python 为 3.13.14，但未安装上述模型依赖。
- 两个 SVR 文件记录的 scikit-learn 版本为 1.0.2；该历史版本与当前 Python 3.13 环境的可安装性和反序列化兼容性尚未验证。
- 源码支持 CUDA 可用时使用 GPU、否则回退 CPU，但没有记录训练所用 PyTorch/CUDA 版本，也没有性能或显存要求。

结论：两个模型能否在同一 Python/PyTorch/CUDA 环境稳定运行，当前为**待人工确认**。阶段 0 不安装依赖、不加载权重、不运行模型。

## 2. 第一阶段 MVP 范围

### 2.1 当前阶段与下一阶段的边界

本文件属于阶段 0。本阶段只定义章程、需求边界、总体架构、技术决策和开发规范，不创建或修改业务实现。

阶段 1 的候选模型工具为：

- `sem_image_generation`
- `sem_mechanical_property_prediction`

工具 ID 保持稳定，不与 `virtual_lab_sem.py`、DDPM checkpoint、DenseNet 或 SVR 文件名绑定。进入阶段 1 前，必须完成本文件列出的待人工确认项和最小契约测试，尤其是用户上传真实 SEM 图像的适用性以及性能预测对四维工艺参数的依赖。

阶段 1 计划通过 Docker Compose 仅管理 PostgreSQL 和 MinIO。FastAPI、前端和两个本地 Predictor 暂时运行在宿主机。阶段 0 不创建 Compose 文件，不启动容器。前端框架保持**待技术决策**，本节不锁定 Vue、Next.js 或其他方案。

### 2.2 MVP 候选用例

#### 用例 A：只生成 SEM 图像

```text
显式四维工艺参数
→ LangChain 提取意图和参数建议
→ Application 严格校验单位、范围和必填项
→ SEMImageGenerationTool
→ SEMImageGeneratorPredictor
→ 生成张量及图像结果
→ AssetService 登记 generated asset
→ 保存 ToolResult 和自然语言解释
```

#### 用例 B：只预测力学性能

目标流程为：

```text
SEM 图像 asset_id + 模型实际要求的其他关键输入
→ SEMMechanicalPropertyPredictionTool
→ SEMMechanicalPropertyPredictor
→ 屈服强度（MPa）与延伸率（%）
```

根据当前代码，“其他关键输入”至少包括 `T1`、`t1`、`T2`、`t2`。在人工确认模型可以不使用这些参数之前，Application 不得省略、默认或由 LLM 猜测这些输入。

用户上传真实 SEM 图像能否用于此用例尚未确认。阶段 1 只有在确认训练域、图像预处理契约和最低验证标准后才能启用该入口；否则该入口必须返回 `NOT_APPLICABLE` 或受控的待确认错误，而不是执行不可靠预测。

#### 用例 C：生成后继续预测

```text
显式四维工艺参数
→ SEM 图像生成 tool_run
→ 生成图片登记为 generated asset_id
→ Application 验证生成资产满足性能工具输入契约
→ SEM 性能预测 tool_run
→ 保存屈服强度、延伸率及单位
→ 生成综合解释
```

当前代码已演示算法级的完整顺序，但阶段 1 平台不得直接把 `VirtualLab.run` 当成一个不可拆分的大工具。两个 Tool、两个 ToolResult 和两段运行记录必须保持独立，由 Application 负责顺序和资产传递。

### 2.3 MVP 非范围

第一阶段 MVP 暂不实现：

- EBSD 图像生成；
- EBSD 性能预测；
- 图像分割；
- 裂纹检测；
- 其他材料工具；
- LangGraph 和多智能体；
- Redis、后台任务队列和可靠事件投递；
- SSE 和 WebSocket；
- 独立模型服务；
- GPU 集群和自动扩缩容；
- 完整可观测平台和完整评价体系；
- 用户、权限、计费和多租户；
- 模型训练和数据标注平台；
- 动态插件安装或数据库动态注册；
- 生产级容器部署。

“不进行生产级部署”不等于完全不使用 Docker；阶段 1 仅以 Docker Compose 管理 PostgreSQL 和 MinIO。

## 3. 模块化单体总体架构

```text
Frontend（框架待定）
   ↓
API
   ↓
Application Services
   ├─ ChatOrchestrationService
   │    └─ LangChain Orchestrator
   │          └─ Tool Catalog View
   │                └─ derived from Tool Registry metadata
   ├─ GenerateSEMImageUseCase
   ├─ PredictSEMPropertiesUseCase
   ├─ GenerateAndPredictSEMUseCase
   ├─ ToolExecutionService
   ├─ AssetService
   ├─ TaskService
   └─ ResultService
          ↓
Core Ports / Registries
   ├─ Tool Registry
   │    ├─ SEMImageGenerationTool
   │    │      └─ SEMImageGeneratorPredictor Port
   │    └─ SEMMechanicalPropertyPredictionTool
   │           └─ SEMMechanicalPropertyPredictor Port
   ├─ StorageService Port
   ├─ Repository Ports
   └─ EventPublisher Port
          ↓
Infrastructure Adapters
   ├─ PostgreSQL Repositories
   ├─ MinIO StorageService
   ├─ Local SEM Generation Predictor
   ├─ Local SEM Property Predictor
   └─ InProcessEventPublisher
```

### 3.1 Application 不是巨型类

Application 是应用层。第一阶段使用三个明确的用例处理器表达固定流程，不引入通用工作流引擎：

- `GenerateSEMImageUseCase`：只生成并登记 SEM 图像。
- `PredictSEMPropertiesUseCase`：只对合格的 SEM 资产执行性能预测。
- `GenerateAndPredictSEMUseCase`：按固定顺序协调前两个能力，并传递生成的 `asset_id`。

`ChatOrchestrationService` 负责取得 LangChain 的结构化意图，再选择允许的 Application Use Case。它不承载资产存储、模型加载或结果持久化细节。底层服务不得反向依赖 `ChatOrchestrationService`。

### 3.2 LangChain 与 Application 的责任边界

LangChain 只负责：

- 理解用户是只生成、只预测、生成后预测，还是普通知识问题；
- 提议一个或两个稳定的 `tool_id`；
- 提取结构化参数建议；
- 标记缺少的必要输入或意图歧义。

LangChain 不选择 `tool_version`、`model_version`，不直接执行函数，也不能自行创建任意调用链。

Application 负责：

- 对所有参数、单位、资产和适用性再次校验；
- 通过 Tool Registry 解析已启用的工具版本和平台模型策略；
- 只允许白名单中的单工具或固定双工具流程；
- 创建并更新 task、tool_run、inference_run、asset 和 result 记录；
- 控制工具顺序、传递 `asset_id`、处理失败与部分成功；
- 在失败后阻止 LLM 编造图像、性能值或替代结果。

### 3.3 Tool、Predictor 与 Registry 约束

- Tool 不得直接调用另一个 Tool。
- Predictor 不得调用另一个 Predictor。
- 两个 Tool 的顺序和数据传递只能由 Application 控制。
- Predictor 不依赖 LangChain、Tool Registry、HTTP、PostgreSQL、MinIO 或其他 Predictor。
- Tool 负责平台契约与 Predictor 契约之间的适配；文件访问必须通过 StorageService。
- Tool Registry 是工具元数据和可执行 Tool 的唯一事实来源。
- Tool Catalog 只是由 Registry 元数据生成的 LangChain 只读投影，不单独维护或持久化。
- MVP 使用代码级静态注册，不实现动态插件安装或数据库注册。

### 3.4 Event 边界

Event 模块定义版本化事件结构和 `EventPublisher` Port。阶段 1 使用 `InProcessEventPublisher`，不承诺可靠投递。后续可以替换为 Redis 发布器，再由 SSE 订阅层推送状态；Application 不依赖 Redis 或 SSE。

## 4. 标识、运行关系与资产溯源

### 4.1 标识定义

- `request_id`：一次 HTTP 请求。
- `conversation_id`：一组关联的多轮交互；不替代任务或执行链路标识。
- `task_id`：稳定的业务任务，可跨一次或多次请求查询。
- `trace_id`：一次实际执行链路。
- `tool_run_id`：任务中的一次逻辑 Tool 执行。
- `inference_run_id`：一次实际 Predictor 调用；一个逻辑 Tool 运行可因重试、回退或模型集成包含多个推理运行。
- `asset_id`：上传、生成或派生文件资产。
- `result_id`：一个持久化结果聚合，可表示单步 ToolResult 或任务级汇总结果。

串联任务至少形成：

```text
task_id / trace_id
├─ tool_run_id: sem_image_generation
│  ├─ inference_run_id
│  ├─ generation_result_id
│  └─ generated asset_id
├─ tool_run_id: sem_mechanical_property_prediction
│  ├─ inference_run_id
│  └─ property_result_id
└─ task_summary_result_id
```

### 4.2 资产模型

`asset_id` 不只表示用户上传文件。`source_type` 至少支持：

- `uploaded`
- `generated`
- `derived`

资产元数据预留：

- `asset_id`、`asset_type`、`source_type`；
- `original_filename`（如存在）、`mime_type`、`object_key`；
- `created_by_tool_run_id`（工具生成时）；
- `parent_asset_id` 或 `source_asset_ids`（派生资产时）；
- `model_id`、`model_version`；
- `preprocessing_version`、`postprocessing_version`；
- `created_at`、内容哈希或校验值；
- 所属 `task_id`；
- 用户或租户关联字段仅预留，不在 MVP 实现用户系统。

生成资产必须先由 StorageService 保存，再由 AssetService 登记元数据。业务模块不得直接使用本地路径或 MinIO SDK。

完整溯源链为：

```text
显式工艺参数
→ generation tool_run_id
→ generation inference_run_id
→ generation ToolResult
→ generated asset_id
→ property tool_run_id
→ property inference_run_id
→ property result_id
→ task summary / explanation
```

中间 SEM 图像、生成条件和第一步结果不得因第二步成功或失败而丢失。

## 5. 三层结果保存

系统必须分别保存：

1. `PredictorRawOutput`：成功推理的必要原始数值、摘要和文件引用；不默认保存完整中间张量或激活值。
2. `ToolResult`：Tool 标准化后的平台结果。
3. `NaturalLanguageExplanation`：基于已保存 ToolResult 生成的解释，单独记录 LLM/提示词版本和生成状态。

Predictor 失败信息写入对应 `inference_run` 的结构化错误字段，不伪装为成功的 `PredictorRawOutput`。

### 5.1 SEM 图像生成 ToolResult

至少预留：

```text
status
data.process_parameters
data.generation_config
artifacts[].asset_id
warnings
applicability
tool_id / tool_version
model_id / model_version
preprocessing_version / postprocessing_version
schema_version
provenance
```

如果使用随机性，`generation_config` 必须记录实际种子、采样数量、CFG 参数和采样步数。当前源码虽计算内部种子，但没有返回它，阶段 1 适配时必须显式化。

### 5.2 SEM 力学性能预测 ToolResult

根据当前真实模型，成功结果应显式表示：

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

同时包含输入图像 `asset_id`、实际使用的四维工艺参数、模型及预处理版本、`warnings`、`confidence`、`applicability`、`out_of_distribution` 和运行溯源。当前模型未提供置信度/OOD 算法时，相应字段必须明确为 `null`/`unknown`，不得编造数值。

## 6. 失败与部分成功语义

任务状态预留 `PARTIALLY_SUCCEEDED`。只有当至少一个必需步骤已经成功并产生可持久化、可使用的结果，而后续必需步骤失败时，才能使用该终态。

规则如下：

1. 工艺参数不完整时，不执行生成 Tool；任务进入 `NEEDS_INPUT`。
2. 请求性能预测但缺少图像或当前模型要求的四维工艺参数时，不执行性能 Tool；不得静默补全。
3. 上传文件损坏、格式不支持或无法转换为确认的输入契约时，不调用 Predictor。
4. 生成失败时，第一项 tool_run 和任务标记 `FAILED`，不执行性能预测。
5. 生成成功但性能预测失败时：第一项 tool_run 保持 `SUCCEEDED`；生成资产和结果保留；第二项 tool_run 标记 `FAILED`；任务标记 `PARTIALLY_SUCCEEDED`。
6. Application 在调用第二个 Tool 前必须校验生成资产的类型、预处理版本和适用性；不得把任意生成图像自动视为有效输入。
7. 用户上传真实 SEM 图像未通过适用性准入时，返回 `NOT_APPLICABLE` 或受控错误，不进行预测。
8. LangChain 无法区分三种用例时必须追问；提议不存在的 Tool、非法参数或未启用组合时由 Application 拒绝。
9. Tool 或 Predictor 失败后，LLM 只能解释失败与已保留的真实部分结果，不得编造图像、性能值或替代结果。

## 7. 最小验收场景

阶段 1 实现前，以下场景应先转化为可执行的契约测试与验收标准：

1. 提供有效四维工艺参数，只生成 SEM 图像。
2. 上传经确认适用的 SEM 图像并提供模型要求的关键输入，只预测屈服强度和延伸率。
3. 提供有效工艺参数，先生成 SEM 图像，再预测力学性能。
4. 普通知识问题，不创建 tool_run 或 inference_run。
5. 请求生成但缺少必要工艺参数，进入 `NEEDS_INPUT`。
6. 请求预测但没有 SEM 图像，不执行性能 Tool。
7. 上传文件损坏或格式不支持，不调用 Predictor。
8. SEM 图像生成失败，不继续性能预测。
9. 生成成功但性能预测失败，保留生成资产并标记 `PARTIALLY_SUCCEEDED`。
10. 用户上传图像不满足性能模型适用范围，返回明确的不可适用结果。
11. LLM 提议不存在的 Tool、非法参数或非法顺序时，由 Application 拒绝。
12. 所有成功数值结果携带明确单位、模型版本和输入资产溯源。

其中场景 2 和场景 10 在“真实 SEM 图像适用性”得到人工确认前属于阻塞性验收门槛，不能用生成图像测试替代。

## 8. 待人工确认清单

进入阶段 1 前必须确认：

1. `ZTA35G_lab` 对应的准确材料牌号、训练数据来源和适用材料范围。
2. 四维参数 `T1/t1/T2/t2` 的业务定义是否与代码注释完全一致，以及是否允许范围外输入。
3. 生成模型训练所用数据、PyTorch/Torchvision/CUDA/Python 版本和最低显存/运行时间。
4. DDPM checkpoint 应使用 `ema` 还是 `model`，以及 `strict=False` 是否产生缺失或多余键。
5. 性能预测是否必须始终携带四维工艺参数；若是，工具名称和输入 Schema 必须明确体现。
6. 两个 SVR、DenseNet 权重和特征预处理的训练版本与兼容依赖。
7. 用户上传真实 SEM 图像是否在训练/验证域内；需要的灰度、尺寸、位深、裁剪、缩放和归一化规则。
8. 性能预测是针对单张图，还是必须对多张同条件图像聚合；当前代码会跨样本取均值。
9. 生成样本应分别保存为资产，还是保存组合图并附样本清单。
10. 当前 Python 3.13.14 环境的依赖版本方案；阶段 0 不下载或安装包。

## 9. 术语与旧设计检查

- 本文没有把 EBSD 性能预测作为 MVP；EBSD 只在“第一阶段非范围”中出现。
- 工具稳定 ID 使用 `sem_image_generation` 和 `sem_mechanical_property_prediction`，不使用模型文件名。
- `Tool Registry` 是唯一事实来源；`Tool Catalog View` 是其只读投影。
- “工作流”仅指 Application 中三个固定用例处理器，不表示引入 LangGraph 或通用工作流引擎。
- 前端框架未锁定。
- 当前仓库在创建本文前没有其他阶段 0 文档，未发现需要直接修订的旧 MVP 文本。
- `virtual_lab_sem.py` 中的 `VirtualLab`、`Generated_Results`、本地路径和“一次执行完整链路”等词属于原始模型实现事实，只在审查记录中保留，不作为平台模块名称或目标架构。
