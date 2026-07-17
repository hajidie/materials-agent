# 材料智能体平台阶段 0 第四节 B：本地 ZTA35G Tool Runtime 通信契约设计

> 状态：已确认设计基线
>
> 确认日期：2026-07-17
>
> 前置基线：第一节、第二节、第三节和第四节 A 均为“已确认设计基线”
>
> 适用范围：`materialsagent-backend` 经本地 Tool Client Adapter 调用 `materialsagent-zta35g` Runtime 的内部通信
>
> 事实依据：本轮重新检查当前仓库实际状态，并用 UTF-8 完整读取第一节、第二节、第三节、第四节 A 和第四节 B 最新文档。本轮只对第四节 B 做最后一致性修订并新增独立阶段 1 实施计划；不修改第一、第二、第三节、第四节 A 正文，不修改、移动或运行 `SEM/`，不加载权重、不安装依赖、不创建 Runtime/API/Adapter 代码、不创建数据库或部署配置，也不执行 git commit。

## 项目负责人审阅层

本节只解释后端平台怎样调用本机旧模型环境。它不要求项目负责人理解 NumPy 字节、HTTP 客户端库、进程锁或 Conda 命令，只需要确认下面的产品和运维边界。

### 1. 为什么后端和模型需要两个环境

平台后端候选使用 Python 3.11 和较新的 FastAPI、LangChain、Pydantic、SQLAlchemy 等依赖；现有 ZTA35G 模型代码以 Python 3.8、旧版 PyTorch/Torchvision/CUDA、NumPy、Joblib 和 scikit-learn 组合为兼容起点。强行把两套依赖装进同一个环境，会提高权重无法加载、序列化模型不兼容和平台依赖互相冲突的风险。

因此 MVP 使用两个隔离环境：

```text
materialsagent-backend
→ 平台、数据库、MinIO、聊天和公共 API

materialsagent-zta35g
→ DDPM、DenseNet121、两个 SVR 和旧依赖
```

### 2. 本地 Runtime 是什么

本地 Runtime 是一个只在本机运行的轻量模型进程。它启动时加载 DDPM、DenseNet121 和两个 SVR，加载成功后保持运行，后续请求复用同一组模型。它只执行当前一个 `zta35g_sem_virtual_lab` Tool，不是通用 Tool 平台，也不表示整个系统改成微服务架构。

### 3. 用户和前端能否直接访问 Runtime

不能。Runtime 只监听 `127.0.0.1`，不放入公共 `/api/v1`，不向浏览器、前端或外部调用者公开。用户仍只访问第四节 A 定义的公共 API；后端内部通过 Tool Client Adapter 调用 Runtime。

### 4. 后端会发送什么

后端只发送一次 Tool 执行所需的最小数据：

- `request_id`、`task_id`、`tool_run_id`；
- 固定的 Tool、Tool 版本和 Schema 版本；
- 已经由 Application 校验的四维工艺参数；
- `requested_outputs`；
- 实际运行参数：平台为本次执行生成并记录的 seed，以及 MVP 受控配置 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000`。这些固定值由 Backend 明确发送，供 Runtime 校验和回显；普通用户不能通过公共 API 修改。

后端不发送用户身份、完整对话、数据库连接、MinIO 地址、object key、权重路径或任意 Python 参数。

### 5. Runtime 会返回什么

Runtime 返回：

- 本次执行是否成功、部分成功或失败；
- 哪些请求输出已经完成、哪些失败；
- 力学性能结构化结果；
- 一张可供后端正式编码 PNG 的框架中立灰度图像载荷；
- 主要步骤 diagnostics、实际运行参数、warning 和安全错误摘要。

Runtime 不直接上传 MinIO，不返回正式 PNG，不返回完整 Python 堆栈、权重路径或模型内部对象。Backend/AssetService 继续负责正式 PNG 量化、编码、SHA-256、MinIO 保存和 Asset 状态。

### 6. 模型是否每次重新加载

不重新加载。Runtime 进程启动时加载模型一次，成功后进入 ready；每次 execute 复用已加载模型。每请求重新加载大权重既慢又容易造成显存抖动，不属于 MVP 正常行为。

### 7. Runtime 失败、繁忙或超时时用户会看到什么

用户不会看到 Runtime 内部异常。Backend 把本地通信和模型错误规范化为第四节 A 的安全公共错误：

- Runtime 未就绪或模型加载失败：ToolRun/Task 失败，通常为 HTTP 503；
- Runtime 正忙：默认立即返回 `RUNTIME_BUSY`，Backend 形成稳定失败，不建立无界队列；
- Runtime 超时或无响应：ToolRun/Task 失败，公共 HTTP 可以为 504，错误码为 `TOOL_RUNTIME_TIMEOUT`；
- 性能预测失败但 SEM 已成功且是用户请求输出：尽可能保留 SEM，Task 可以部分成功；
- ToolResult 已成功而 Explanation 超时：按第四节 A 保留结构化结果，Task 部分成功。

### 8. 如何通过日志判断故障在后端还是模型环境

Backend 和 Runtime 共同记录 `request_id`、`task_id`、`tool_run_id`。若 Backend 没有发出调用，故障在请求、校验或 Adapter 之前；若 Runtime 没有 `execution_received`，故障在 Adapter 或本地传输；若 Runtime 已记录 `sem_generation` 或 `mechanical_property_prediction` 失败，故障在模型环境；若 Runtime 已完成但 Backend 报响应解析或 Asset 保存失败，故障在平台响应处理或持久化。

### 9. 未来迁移远程 GPU 为什么不需要重写平台

Application、MaterialTool、Task、ToolRun、ToolResult、Asset、Explanation 和公共 `/api/v1` 都不依赖具体 HTTP 客户端。未来迁移远程 GPU 时，替换或配置 Tool Client Adapter，继续复用 `/internal/v1` 语义，再增加 TLS、正式服务认证、网络超时和部署管理即可。前端仍不知道 Runtime URL，Runtime 也不会注册成公共 Tool。

### 10. 项目负责人如何验收

项目负责人需要确认：

1. Runtime 只监听 `127.0.0.1`，浏览器不能直接访问。
2. 后端和模型环境相互隔离，模型代码不注入 backend 环境。
3. Runtime 启动时加载模型一次，多次请求复用。
4. Runtime 只接受固定 ZTA35G Tool 和受控字段。
5. Backend 仍负责正式 PNG 和 MinIO，Runtime 不获得存储信息。
6. execute 在公共请求内同步等待，默认不自动重试。
7. 最大并发默认是 1，繁忙时不无界排队。
8. live 不运行推理；ready 不执行完整 DDPM。
9. 错误响应不包含堆栈、权重路径或完整图片 bytes。
10. 日志能用三个 ID 定位后端、传输、模型、解析和持久化故障。
11. 未来远程 GPU 只需要替换 Adapter 和增强传输安全，不重写平台核心。

## 1. 范围与非范围

### 1.1 本节范围

本节只设计：

```text
materialsagent-backend（Python 3.11 候选）
→ Local ZTA35G Tool Client Adapter
→ 127.0.0.1
→ materialsagent-zta35g Runtime（Python 3.8）
```

包括：

- 本地通信方式选择；
- 内部路径与协议版本；
- execute 请求、图像载荷和响应契约；
- Runtime 错误、传输错误和 Backend 规范化边界；
- 同步、超时、客户端断开和重试；
- 单 GPU 最小并发边界；
- Runtime 生命周期与 live/ready；
- loopback、共享 token、载荷限制和安全校验；
- Backend/Runtime 结构化日志关联；
- 未来远程 GPU Adapter 演进。

### 1.2 本节非范围

本节不设计或实现：

- FastAPI、Pydantic、HTTP 客户端、进程启动器或 Runtime 代码；
- SQLAlchemy、Repository、数据库表、DDL 或 migration；
- Docker Compose、Conda 环境、依赖安装或 Windows Service；
- 自动拉起、守护进程、生产级进程管理或远程部署；
- 模型加载实测、权重运行、训练或推理；
- `SEM/` 修改、移动或删除；
- 公共 `/api/v1` 新路径；
- 动态 Tool 管理、模型选择、文件上传、数据库或下载接口；
- Redis、Worker、队列、可靠取消、通用任务调度或服务发现；
- 多 GPU 调度、自动扩缩容或通用微服务平台；
- `model_version`、InferenceRun、StageRun 或模型注册表；
- 阶段 1 实施计划、阶段 1A/1B 实施或 git commit。

### 1.3 本节核心结论

1. MVP 采用 `127.0.0.1` 本地 HTTP。
2. Runtime 内部路径使用 `/internal/v1`，与公共 `/api/v1`、`schema_version`、`tool_version` 独立。
3. execute 使用 JSON；单张图像使用 JSON + Base64 安全 NumPy `.npy` bytes，读取时强制 `allow_pickle=False`。
4. Runtime 内部性能预测使用本次生成的原始图像；响应中的图像只是同一原始结果的跨环境中立副本。
5. Backend/AssetService 继续执行正式 PNG 校验、量化、编码、SHA-256 和 MinIO 保存。
6. execute POST 默认不自动重试；公共幂等由 Backend/Application 处理。
7. Runtime 模型加载一次，默认最大并发为 1，繁忙时默认立即返回 `RUNTIME_BUSY`。
8. Runtime 只绑定 loopback，并默认要求环境变量注入的共享高熵 token。
9. 未来远程 GPU 只替换 Adapter/配置并增加 TLS、正式认证和部署管理。

## 2. 双环境与 Runtime 定位

### 2.1 双环境

```text
Windows 11
├─ materialsagent-backend
│  ├─ Python 3.11 候选
│  ├─ FastAPI / LangChain / Pydantic
│  ├─ SQLAlchemy / PostgreSQL / MinIO
│  └─ Application、MaterialTool、Tool Client Adapter
└─ materialsagent-zta35g
   ├─ Python 3.8
   ├─ PyTorch 1.13.1+cu116 / Torchvision 0.14.1+cu116 候选
   ├─ NumPy 1.22.3 / Joblib 1.4.2 / Matplotlib 3.2.2 候选
   ├─ 涉及 scikit-learn 1.0.2 的 SVR 兼容性
   └─ ZTA35G Tool Runtime
```

上述版本只是阶段 1B 验证起点，不表示已经可运行。

### 2.2 Runtime 的职责

Runtime 负责：

- 启动时读取受控配置；
- 检查模型文件；
- 加载并复用 DDPM、DenseNet121 和两个 SVR；
- 接收已校验工艺参数和受控运行参数；
- 进行防御性边界复核；
- 执行 SEM 生成；
- 按 `requested_outputs` 决定是否执行性能预测；
- 返回结构化执行结果、图像载荷、diagnostics 和安全错误；
- 写本地结构化日志。

Runtime 不负责：

- Actor、Conversation、Message、Task 或 Idempotency-Key；
- PostgreSQL、MinIO、Repository 或 Asset 生命周期；
- LangChain、自然语言解释或公共 HTTP 错误包络；
- Tool Registry、动态 Tool 发现或模型选择；
- 正式 PNG、object key 或文件下载；
- 平台业务重试和幂等。

### 2.3 Tool 与 Runtime 的边界

```text
Application
→ ZTA35GSEMVirtualLabTool.execute(validated_input, request_context)
→ Local ZTA35G Tool Client Adapter
→ Runtime execute
→ Runtime response
→ Adapter 映射为 ToolExecutionOutput
```

`MaterialTool.execute` 仍是平台公共 Tool 契约。Application 和 MaterialTool 不依赖具体 HTTP 库。Tool 可以看到完整 `request_context`，但 Adapter 投影到 Runtime 请求时只保留 `request_id`、`task_id` 和 `tool_run_id`；`conversation_id`、`actor_context`、`actor_id` 和 `user_id` 不跨环境发送。

## 3. 通信方式比较和推荐

### 3.1 候选比较

| 方式 | 跨 Conda/Python | Windows 11 | 调试与健康检查 | 未来远程 GPU | 主要问题 | 结论 |
|---|---|---|---|---|---|---|
| `127.0.0.1` 本地 HTTP | 好 | 原生支持 | 路径、状态码、JSON 和 curl/浏览器工具易排错 | 可复用协议语义，只替换地址与安全层 | 有序列化和本地端口管理成本 | 推荐 |
| 标准输入输出子进程 | 可隔离 | 支持 | stdout 同时承载协议和日志容易混淆；健康检查弱 | 远程迁移需要重写 | 后端需承担进程生命周期，崩溃/阻塞/管道背压复杂 | 不采用 |
| 共享文件 | 可隔离 | 支持 | 可人工查看文件 | 远程共享目录语义差 | 临时路径、清理、原子性、并发和权限复杂；禁止以临时文件路径作跨环境载荷 | 不采用 |
| Unix socket / Windows named pipe | 可隔离 | 平台分裂 | 需要额外平台适配和调试工具 | 不能直接复用为远程网络协议 | Windows named pipe 与 Unix socket 不是同一实现；收益不足 | 不采用 |
| gRPC / Protobuf | 好 | 支持 | 强类型、健康检查可设计 | 适合远程 | 引入 HTTP/2、代码生成和额外依赖，对单 Tool MVP 过重 | 暂不采用 |
| 直接 import 模型代码 | 不隔离 | 支持 | 调用简单 | 无法形成远程边界 | 把旧 PyTorch/CUDA/Joblib 依赖注入 backend 环境 | 拒绝 |

### 3.2 推荐：loopback HTTP

MVP 推荐：

```text
HTTP/1.1
Host: 127.0.0.1:<受控本地端口>
Content-Type: application/json
```

理由：

1. Python 3.11 与 Python 3.8 只通过进程边界交换标准数据。
2. Windows 11 支持成熟，不依赖 Unix 专用能力。
3. live/ready、错误状态、请求体限制和日志容易表达。
4. 可以用普通 HTTP 工具进行本地排错，但端口不对公网开放。
5. 未来远程 GPU 可以继续复用 `/internal/v1` 语义。
6. 模型代码和旧依赖不进入 backend 环境。
7. 当前只有一个 Runtime 和一个 Tool，不需要服务发现、API Gateway、sidecar 或微服务治理。

采用 HTTP 只表示“本机两个兼容环境之间有一个稳定进程边界”，不改变平台的模块化单体定位。

## 4. 内部路径与协议版本

### 4.1 推荐路径

```text
GET  /internal/v1/health/live
GET  /internal/v1/health/ready
POST /internal/v1/execute
```

不提供：

```text
/api/v1/*
/internal/v1/tools/*
/internal/v1/models/*
/internal/v1/uploads/*
/internal/v1/files/*
/internal/v1/database/*
/internal/v1/admin/*
/internal/v1/cancel/*
```

### 4.2 版本独立性

```text
公共平台 API：/api/v1
Runtime 内部协议：/internal/v1
Tool 数据结构：schema_version
Tool 集成行为：tool_version
```

规则：

1. `/internal/v1` 只版本化 Backend Adapter 与 Runtime 的内部通信字段、路径和错误语义。
2. `/api/v1` 只版本化用户和前端看到的公共资源。
3. `schema_version` 约束 ZTA35G Tool 输入、输出和 ToolResult.data。
4. `tool_version` 表示平台对该 Tool 的校验、执行和结果组装版本。
5. 内部协议变更不必自动提升公共 API 版本。
6. Tool 数据破坏性变化不必自动提升 Runtime 路径版本。
7. 当前不引入 `model_version`；可选 `model_bundle_id` 只是内部轻量溯源。

### 4.3 协议字段

内部请求和响应都携带：

```yaml
runtime_contract_version: "1.0"
```

路径主版本 `/internal/v1` 与字段 `"1.0"` 必须兼容。字段可用于日志和响应校验，但不能替代路径版本。

## 5. 执行请求契约

### 5.1 请求结构

```yaml
runtime_contract_version: "1.0"
request_id: req_...
task_id: task_...
tool_run_id: trun_...
tool_id: zta35g_sem_virtual_lab
tool_version: 0.1.0
schema_version: "1.0"
process_parameters:
  solution_temperature: 1000
  solution_time: 3.0
  aging_temperature: 730
  aging_time: 3.0
requested_outputs:
  - sem_image
  - mechanical_properties
runtime_parameters:
  seed: 123456789
  num_samples: 1
  guide_scale: 2.0
  timesteps: 1000
```

MVP 初始受控配置固定为 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000`。seed 由平台为每次执行生成并记录。Backend 仍发送这四个实际值，以便 Runtime 做严格一致性校验并在响应中回显；普通用户不能通过公共 API 修改它们。

### 5.2 必填与类型

| 字段 | 必填 | 规则 |
|---|---|---|
| runtime_contract_version | 是 | 当前为 `"1.0"` |
| request_id | 是 | 不透明字符串，仅用于关联 |
| task_id | 是 | 不透明字符串，仅用于关联 |
| tool_run_id | 是 | 不透明字符串；一次实际执行尝试 |
| tool_id | 是 | 必须等于 `zta35g_sem_virtual_lab` |
| tool_version | 是 | 必须与 Runtime 受支持版本一致 |
| schema_version | 是 | 必须与输入输出 Schema 一致 |
| process_parameters | 是 | 四个标准单位数值字段 |
| requested_outputs | 是 | 非空、去重，只允许 `sem_image`、`mechanical_properties` |
| runtime_parameters.seed | 是 | Backend/Tool 在调用前生成的受控整数 |
| runtime_parameters.num_samples | 是 | MVP 必须等于 `1` |
| runtime_parameters.guide_scale | 是 | MVP 必须精确等于受控 Runtime 配置 `2.0` |
| runtime_parameters.timesteps | 是 | MVP 必须精确等于受控 Runtime 配置 `1000` |

### 5.3 工艺参数

Runtime 接收标准单位：

| 字段 | 类型 | 单位 | 边界 |
|---|---|---|---|
| solution_temperature | integer | °C | 900–1100 |
| solution_time | number，最多 1 位小数 | h | 1–5 |
| aging_temperature | integer | °C | 670–790 |
| aging_time | number，最多 1 位小数 | h | 1–5 |

Application 已经完成主要校验，但 Runtime 必须防御性复核：

- 字段完整；
- 类型正确且为有限值；
- 温度整数、时间精度符合；
- 范围合法；
- requested outputs 合法；
- `num_samples=1`、`guide_scale=2.0`、`timesteps=1000` 与当前受控 Runtime 配置逐项一致；
- 运行参数不存在 NaN/Inf；
- tool/version/schema 兼容。

Runtime 复核失败时不得进入模型执行。它不得把另一个“仍在合法范围内”的 `num_samples`、`guide_scale` 或 `timesteps` 静默接受为本次配置，也不得自行修正或回退；不一致必须返回 `INVALID_RUNTIME_REQUEST` 或等价明确错误。阶段 1B 只验证这组固定配置能否正确运行；若源码真实要求与当前值不同，必须先更新兼容性验收记录和设计基线，再修改 Backend/Runtime 配置。

### 5.4 禁止字段

请求不得包含：

```text
actor_id
user_id
actor_context
conversation_id
完整 Conversation / Message
Idempotency-Key
database_url
PostgreSQL 连接
MinIO endpoint / bucket / object_key / credential
模型权重路径
设备文件路径
Python 模块名 / 函数名 / 类名
任意 kwargs / eval 内容
pickle 对象
图片临时文件路径
```

Runtime URL 只能来自 Backend 启动配置，公共请求不能覆盖。

### 5.5 请求大小

MVP 内部 execute JSON 请求体上限固定为：

```text
64 KiB
```

当前字段远小于该上限。Runtime 和 Adapter 都应在反序列化任意深层对象前实施大小保护；超限返回 `INVALID_RUNTIME_REQUEST`。

## 6. 图像载荷协议

### 6.1 已确认边界

1. Runtime 内部性能预测使用本次 DDPM 生成的原始图像，不先经过 PNG。
2. Runtime 返回同一原始图像的框架中立载荷，供 Backend/AssetService 检查并正式编码 PNG。
3. 公共 ToolExecutionOutput 和内部协议都不暴露 `torch.Tensor` 或 CUDA Tensor。
4. Runtime 不直接上传 MinIO。
5. 正式 PNG 继续使用第二节已确认的量化与编码责任。

### 6.2 候选比较

| 方案 | 优点 | 问题 | 结论 |
|---|---|---|---|
| JSON 嵌套浮点数组 | 最直观，无二进制容器 | 262,144 个 JSON 数值体积大、解析慢、精度表示冗长 | 不采用 |
| JSON + Base64 原始 float32 bytes | 体积可控，实现简单 | shape/dtype/order 需要完全依赖外部元数据，裸 bytes 易被错误解释 | 可用但不优先 |
| JSON + Base64 安全 `.npy` bytes | `.npy` 自带 dtype/shape/order；NumPy 双环境容易实现；无需临时文件 | Base64 增加约 33% 体积；必须禁用 pickle 并复核元数据 | MVP 推荐 |
| multipart JSON + 二进制 | 无 Base64 开销，适合大文件 | 请求/响应解析、错误处理和测试更复杂；当前仅单图收益有限 | 未来可选 |
| Runtime 直接返回最终 PNG | 网络载荷最小，浏览方便 | 把正式量化/PNG 编码责任从 AssetService 移到 Runtime，与既有基线冲突 | 拒绝 |

### 6.3 推荐格式

```yaml
images:
  - image_role: generated_sem
    requested_output: true
    dtype: float32
    numpy_dtype: "<f4"
    shape: [512, 512]
    channel_layout: GRAYSCALE_2D
    value_range: [-1.0, 1.0]
    encoding: base64+npy
    byte_order: little
    array_order: C
    sha256: "<可选；对解码后的完整 .npy bytes 计算>"
    data_base64: "<完整 .npy bytes 的 Base64>"
```

只请求性能时，同一载荷使用：

```yaml
image_role: intermediate_sem
requested_output: false
```

### 6.4 编码与解码规则

Runtime：

1. 从本次原始生成图像构造 CPU NumPy 数组，不返回 Tensor。
2. 数组必须是二维单通道、C order、`float32`、预期 shape `[512, 512]`。
3. 拒绝 NaN、`+Inf`、`-Inf`。
4. 声明预期值域 `[-1.0, 1.0]`。
5. 使用 NumPy `.npy` 格式写入内存 bytes，不使用临时文件。
6. Base64 编码完整 `.npy` bytes。
7. 可选计算 `.npy` bytes 的 SHA-256。

Backend/Adapter：

1. 先检查整个 HTTP 响应体大小。
2. 检查 Base64 字符和解码后大小。
3. 若有 sha256，先核对完整 `.npy` bytes。
4. 使用 `numpy.load(BytesIO(...), allow_pickle=False)` 或等价安全读取。
5. 拒绝 object dtype、structured object、额外数组或 pickle。
6. 复核 dtype、shape、维数、array order 和有限值。
7. 检查实际 min/max；若超出阶段 1B 确认的数值容差，返回 `INVALID_MODEL_OUTPUT`。
8. 对容差内的边界值仍按第二节正式 PNG 规则执行 clip、映射、四舍五入和 uint8 编码。
9. 不把 `.npy` bytes、Base64 字符串或临时路径保存进 PostgreSQL。

### 6.5 大小与 Base64 开销

单张 `512×512`、`float32` 图像的原始数组大小为：

```text
512 × 512 × 4 bytes = 1,048,576 bytes ≈ 1 MiB
```

`.npy` 只增加很小的头部；Base64 约增加三分之一，图像字段约为 `1.34 MiB`。在本机 loopback、单张图片和低并发 MVP 下可接受，换来更简单的 JSON 协议和 dtype/shape 自描述。

MVP execute 响应体上限固定为：

```text
4 MiB
```

它足以容纳一张 Base64 `.npy`、性能结果、warnings、diagnostics 和错误。超过上限由 Adapter 视为 `INVALID_MODEL_OUTPUT` 或 `TOOL_RUNTIME_RESPONSE_INVALID`，不得静默截断。未来多图、远程 GPU 或更大数组时重新评估 multipart 或受控对象传输。

## 7. 执行响应契约

### 7.1 成功或可规范化失败响应

```yaml
runtime_contract_version: "1.0"
request_id: req_...
task_id: task_...
tool_run_id: trun_...
tool_id: zta35g_sem_virtual_lab
tool_version: 0.1.0
schema_version: "1.0"
status: SUCCEEDED | PARTIALLY_SUCCEEDED | FAILED
requested_outputs:
  - sem_image
  - mechanical_properties
completed_outputs:
  - sem_image
failed_outputs:
  - mechanical_properties
data: {}
images:
  - <ImagePayload>
warnings: []
diagnostics:
  - step: sem_generation
    status: SUCCEEDED
    started_at: "..."
    completed_at: "..."
    duration_ms: 1234
    error_code: null
    safe_error_message: null
  - step: mechanical_property_prediction
    status: FAILED
    started_at: "..."
    completed_at: "..."
    duration_ms: 234
    error_code: MECHANICAL_PROPERTY_PREDICTION_FAILED
    safe_error_message: "力学性能预测未完成。"
actual_runtime_parameters:
  seed: 123456789
  num_samples: 1
  guide_scale: 2.0
  timesteps: 1000
model_bundle_id: zta35g-sem-original-bundle
error:
  code: MECHANICAL_PROPERTY_PREDICTION_FAILED
  safe_message: "力学性能预测未完成。"
  retryable: false
```

示例性能数值、运行参数和耗时只展示结构，不代表实际模型输出、配置或评价结果。

### 7.2 字段语义

- `runtime_contract_version`：内部协议版本。
- 三个关联 ID：必须原样回显，Adapter 必须核对。
- `tool_id/tool_version/schema_version`：必须与请求和 Runtime 支持项一致。
- `status`：Runtime 可表达的执行结果，不是公共 Task 状态的替代品。
- `requested_outputs`：必须等于请求。
- `completed_outputs/failed_outputs`：互斥，且均为 requested 子集。
- `data`：只含 ZTA35G Tool 受控结果。
- `images`：使用第 6 节内部载荷。
- `warnings`：安全、结构化、数量有界。
- `diagnostics`：只包含主要步骤，不建立 StageRun。
- `actual_runtime_parameters`：Runtime 实际使用的 seed 和固定配置；必须与请求及受控 Runtime 配置一致。
- `model_bundle_id`：可选内部轻量溯源，只识别当前接入的文件包，不进入普通用户公共响应。文件实际身份与完整性由各权重文件 SHA-256 记录；不建设模型发布、灰度、回滚或动态版本选择系统。
- `error`：稳定错误码和安全摘要，不包含堆栈。

### 7.3 三种请求模式

只请求 `sem_image`：

```text
completed_outputs = [sem_image]
failed_outputs = []
images = [generated_sem]
data.mechanical_properties 不存在或为空
```

只请求 `mechanical_properties`：

```text
Runtime 仍生成 SEM 并用于性能预测
images = [intermediate_sem]
性能成功时 completed_outputs = [mechanical_properties]
```

性能成功时 `data` 与公共 ToolResult 语义一致，例如：

```yaml
data:
  yield_strength:
    value: 650.0
    unit: MPa
  elongation:
    value: 3.2
    unit: "%"
```

示例性能数值只展示结构，不代表实际模型输出或评价结果。

同时请求两项：

```text
只生成一张 SEM
同一原始图像用于性能预测和 images 返回
```

### 7.4 部分失败

若 SEM 已生成、性能预测发生可捕获异常：

- 同时请求两项：返回 SEM，status `PARTIALLY_SUCCEEDED`，completed 为 `sem_image`，failed 为 `mechanical_properties`。
- 只请求性能：可以返回 intermediate SEM 用于追溯，但 completed 为空，status `FAILED`。
- diagnostics 明确 `sem_generation` 成功和 `mechanical_property_prediction` 失败。

进程崩溃、GPU 进程直接退出或宿主机故障时允许没有结构化响应。Adapter 此时按传输/进程失败规范化，不能伪造 Runtime 已完成字段。

### 7.5 禁止响应内容

Runtime 不返回：

```text
object_key
bucket
数据库 ID 之外的平台内部关系
数据库连接或 MinIO 信息
actor_id / user_id
完整 traceback
异常对象 repr
权重绝对路径
完整模型对象
Tensor / CUDA Tensor
DenseNet 特征 / SVR 中间值
完整图片日志
```

## 8. 错误协议

### 8.1 Runtime 错误结构

```yaml
error:
  code: RUNTIME_NOT_READY
  safe_message: "ZTA35G Runtime 尚未就绪。"
  retryable: true
  failed_step: model_loading | sem_generation | mechanical_property_prediction | null
  details: {}
```

`details` 只能包含受控、小型、安全字段，例如不支持的协议版本或字段名，不能包含任意 debug JSON。

### 8.2 必须支持的错误码

```text
INVALID_RUNTIME_REQUEST
UNSUPPORTED_TOOL
SCHEMA_VERSION_MISMATCH
TOOL_VERSION_MISMATCH
RUNTIME_NOT_READY
RUNTIME_BUSY
MODEL_LOAD_FAILED
SEM_GENERATION_FAILED
MECHANICAL_PROPERTY_PREDICTION_FAILED
INVALID_MODEL_OUTPUT
INTERNAL_RUNTIME_ERROR
```

### 8.3 分层错误分类

| 层级 | 典型现象 | Runtime 是否有结构化响应 | Backend 规范化 |
|---|---|---|---|
| HTTP/传输失败 | connection refused、连接重置、DNS 不适用、响应截断 | 否或不完整 | `TOOL_RUNTIME_UNREACHABLE` / `TOOL_RUNTIME_PROTOCOL_ERROR` |
| 请求校验失败 | JSON、字段、范围、tool/version/schema 不合法 | 是 | ToolRun/Task FAILED，安全映射 |
| 尚未加载完成 | 进程存活但 model_loaded=false | 是 | `RUNTIME_NOT_READY`，通常 HTTP 503 |
| Runtime 繁忙 | 唯一执行槽被占用 | 是 | `RUNTIME_BUSY`，通常 HTTP 503 |
| 模型加载失败 | 启动加载异常 | health 有安全摘要 | `MODEL_LOAD_FAILED`，Tool 不可用 |
| SEM 生成失败 | 可捕获模型异常或输出不合法 | 尽量有 | `SEM_GENERATION_FAILED` / `INVALID_MODEL_OUTPUT` |
| 性能预测失败 | DenseNet/SVR 可捕获异常 | 尽量有，可带 SEM | `MECHANICAL_PROPERTY_PREDICTION_FAILED` |
| 返回载荷非法 | ID/版本不符、Base64/NPY/shape/dtype/value invalid | Runtime 自身可能认为成功 | Adapter 改为 `TOOL_RUNTIME_RESPONSE_INVALID` / `INVALID_MODEL_OUTPUT` |
| Runtime 崩溃或无响应 | 进程退出、GPU fatal、请求超时 | 否 | `TOOL_RUNTIME_TIMEOUT` 或进程/传输失败 |

### 8.4 Runtime HTTP 状态

| 场景 | Runtime HTTP | 响应 |
|---|---:|---|
| live/ready 成功 | 200 | 安全健康结构 |
| execute 形成成功、部分成功或可规范化模型失败 | 200 | 完整执行响应，status 区分 |
| JSON 无法解析 | 400 | `INVALID_RUNTIME_REQUEST` |
| Content-Type 不支持 | 415 | `INVALID_RUNTIME_REQUEST` |
| 字段、范围或 Tool 不支持 | 422 | `INVALID_RUNTIME_REQUEST` / `UNSUPPORTED_TOOL` |
| tool/schema/runtime contract 不兼容 | 409 | 对应 mismatch 错误 |
| 未就绪、繁忙或模型加载失败 | 503 | 对应安全错误 |
| 捕获到 Runtime 自身未预期内部异常 | 500 | `INTERNAL_RUNTIME_ERROR` |

Backend 不把 Runtime HTTP 状态原样透传给公共 API，而是按第四节 A 的业务状态、已持久化事实和公共错误码规范化。

### 8.5 traceback

完整 Python traceback 只进入 Runtime 本地受限日志。返回 Backend 的响应只包含稳定错误码、安全摘要、失败步骤和少量安全详情。Backend 公共 API 再次过滤，绝不返回 traceback。

## 9. 同步、超时、客户端断开与重试

### 9.1 同步调用

```text
公共写入 POST
→ Backend/Application
→ MaterialTool.execute
→ Adapter POST /internal/v1/execute
→ 同步等待 Runtime
→ 持久化 ToolRun/Asset/ToolResult/Explanation
→ 返回稳定业务状态
```

当前不使用队列、Worker、可靠后台任务或 Runtime 内部任务 ID。Runtime execute 不返回 `202`。

### 9.2 超时

第四节 B 不固定具体秒数。阶段 1B 必须测量：

- Runtime 启动和模型加载时间；
- warm execute 的 SEM 生成时间；
- 性能预测时间；
- 完整请求 P50/P95/P99；
- Base64 `.npy` 编码、传输和解析时间；
- GPU 忙、模型挂起和进程退出行为。

Backend 拥有公共调用总超时。达到超时后：

1. Backend 将 ToolRun/Task 规范化为 `FAILED`。
2. 公共 HTTP 可以返回 504 `TOOL_RUNTIME_TIMEOUT`。
3. 未持久化图片和性能不得返回。
4. Runtime 可能仍在计算；Backend 不把迟到响应重新改写进已失败 ToolRun。
5. Runtime 在计算结束前继续占用唯一执行槽，后续 execute 返回 `RUNTIME_BUSY`。
6. 若 CUDA/模型永久挂起，阶段 0 只允许人工判断并重启 Runtime；不预建可靠 watchdog、自动杀进程或恢复队列。

### 9.3 自动重试

- `GET /health/live` 和 `GET /health/ready` 可以由 Adapter 做有限、短间隔重试。
- `POST /execute` 默认不得自动重试。
- 即使是连接重置或超时，只要执行结果不确定，也不能使用同一个 ToolRun 静默再次运行随机模型。
- 公共显式 Tool 重试创建新的 `tool_run_id` 和新的 seed。
- Runtime 不承担平台业务幂等，不保存 `Idempotency-Key`，不根据 request_id 去重执行。
- 公共 Idempotency-Key 由 Backend/Application 在调用 Runtime 前处理；命中重放时不得再次调用 execute。

### 9.4 客户端断开

1. 浏览器或公共 API 客户端断开不等于取消。
2. Backend 是否继续 Application 工作、如何避免 ASGI 取消传播，留阶段 1A 验证。
3. 若 Backend 已把 execute 发送给 Runtime，Runtime 可能继续计算，即使公共连接已断开。
4. 客户端不确定结果时复用同一公共 `Idempotency-Key`，读取原资源当前事实。
5. 不增加 Runtime cancel 路径，不向 Runtime 发送用户取消命令。
6. 不承诺客户端断开后可靠后台完成；只记录真实持久化结果和故障。

### 9.5 阶段 1A 与 1B 验证分界

阶段 1A：

- 公共请求断开和 ASGI 取消传播；
- 幂等重放不重复调用 Adapter；
- Adapter 超时如何终结 ToolRun/Task；
- 迟到响应被丢弃且不覆盖稳定失败；
- Tool Runtime 错误到第四节 A 公共错误的映射。

阶段 1B：

- 真实模型耗时和合理超时值；
- 模型/CUDA 挂起时进程是否恢复；
- 超时后 Runtime 是否持续 busy；
- 是否需要人工重启；
- health 短重试和 execute 不重试的实际行为。

## 10. 并发与资源限制

### 10.1 硬件边界

当前 GPU：

```text
RTX 4060 Laptop
8 GB VRAM
```

DDPM、DenseNet 和 SVR 权重较大，阶段 0 不假定可以并发执行多个生成任务。

### 10.2 推荐 MVP 默认语义

```text
模型加载实例数：1
最大 execute 并发：1
Runtime 内部任务队列：无
繁忙策略：立即返回 RUNTIME_BUSY
无界排队：禁止
```

Runtime 进入执行临界区前尝试获得唯一执行槽；槽已占用时不接收第二个模型执行，不创建 Runtime 内部排队任务，直接返回 HTTP 503 + `RUNTIME_BUSY`，并固定 `error.retryable=true`。`retryable` 只表示允许用户稍后显式重试；Backend 不得自动重试 execute，显式重试必须创建新的 `tool_run_id` 和 seed。

推荐立即拒绝的原因：

- 不把 Runtime 变成未设计的队列系统；
- Backend 可以立即形成稳定失败并让用户显式重试；
- 避免请求在内存中无界等待；
- 超时、客户端断开和进程重启行为更容易判断；
- 8 GB 显存下优先保证单次执行可预测。

### 10.3 阶段 1B 实测后可选调整

阶段 1B 可以比较：

- 立即拒绝；
- 一个严格有界、很短的执行槽等待；
- Adapter 在发出请求前根据 ready/busy 做快速拒绝。

只有显存、耗时和用户体验实测证明有价值时，才允许从“立即拒绝”改为“有界等待”。等待长度必须有限且小于 Backend 总超时，不得形成 Runtime 通用队列。

### 10.4 明确不设计

- 多 GPU 调度；
- batch 合并；
- 自动扩缩容；
- 优先级队列；
- 公平调度；
- 并发 seed 管理器；
- 分布式锁；
- Runtime 内部任务查询或取消。

## 11. Runtime 生命周期

### 11.1 生命周期

```text
进程启动
→ 读取受控配置
→ 绑定 127.0.0.1
→ 检查模型文件和 fingerprint
→ 选择并检查设备
→ 加载 DDPM
→ 记录 missing/unexpected keys
→ 加载 DenseNet121
→ 记录兼容性转换结果
→ 加载两个 SVR
→ 完成最小非推理自检
→ ready
→ 多次请求复用模型
→ 停止接收新请求
→ 当前请求完成或进程被人工终止
→ 关闭
```

### 11.2 模型加载

- 模型加载只发生在启动阶段。
- ready 前不接受 execute。
- 加载失败时 Runtime 进程可以保持 live 以暴露安全 ready 错误，或直接退出；具体进程策略留阶段 1B 验证。
- 无论哪种策略，Backend 都必须把 Tool 标记为不可用。
- missing/unexpected keys、依赖版本和 device 进入本地受限日志与兼容性验收记录，不进入公共 API。

### 11.3 请求复用

每次 execute：

- 使用同一模型实例；
- 使用请求指定且受控的 seed 和运行参数；
- 不修改权重；
- 不将上次请求 Tensor、特征或结果作为下次请求输入；
- 请求结束后释放不再需要的中间对象；
- 是否调用 CUDA cache 清理只能由阶段 1B 显存测试决定，不能每次盲目重载模型。

### 11.4 阶段 0 不实现

不实现 Windows Service、计划任务自动启动、守护进程、自动重启、蓝绿发布、健康探针编排或生产部署。阶段 1B 初次集成采用手动启动：开发者先启动 `materialsagent-zta35g` Runtime，检查 `/internal/v1/health/ready`，再启动 `materialsagent-backend`。Backend 不自动创建 Conda 子进程，不自动启动或重启 Runtime，也不管理其生命周期。

## 12. live 与 ready

### 12.1 live

```text
GET /internal/v1/health/live
```

只说明 Runtime 进程和 HTTP 服务能够响应，不访问数据库、不检查 MinIO、不运行模型推理。

```yaml
runtime_contract_version: "1.0"
status: LIVE
process_started_at: "..."
checked_at: "..."
```

即使模型未加载成功，进程仍存活时 live 可以是 200；模型能力由 ready 表达。

### 12.2 ready

```text
GET /internal/v1/health/ready
```

至少返回：

```yaml
runtime_contract_version: "1.0"
status: READY | NOT_READY
model_files:
  status: AVAILABLE | MISSING | INVALID
model_loaded: true | false
device:
  status: AVAILABLE | UNAVAILABLE
  kind: cuda | cpu | unknown
can_accept_execution: true | false
busy: true | false
supported_tool:
  tool_id: zta35g_sem_virtual_lab
  tool_version: 0.1.0
  schema_version: "1.0"
model_bundle_id: zta35g-sem-original-bundle | null
checked_at: "..."
error:
  code: MODEL_LOAD_FAILED | null
  safe_message: "..." | null
```

规则：

- 模型文件缺失、加载失败或设备不可用：`NOT_READY`，HTTP 503。
- 模型已加载但当前忙：`status=READY`、`busy=true`、`can_accept_execution=false`，HTTP 200；execute 仍返回 `RUNTIME_BUSY`。
- ready 不运行完整 DDPM，不生成 SEM，不执行 DenseNet/SVR。
- 可做的最小自检只能是配置、对象存在性、设备状态和轻量 shape/接口检查。
- 不返回权重绝对路径、主机密钥、traceback 或内部端口之外的信息。

### 12.3 Backend 公共映射

第四节 A 的 `/api/v1/health/ready` 只显示安全组件摘要：

```text
zta35g_sem_virtual_lab = AVAILABLE | UNAVAILABLE | DEGRADED
```

Backend 不把 Runtime ready 原始结构、端口、模型文件名或 bundle 细节直接透传给普通用户。

## 13. 安全边界

### 13.1 loopback

Runtime 必须：

- 只绑定 `127.0.0.1`；
- 不绑定 `0.0.0.0`、局域网 IP 或公网地址；
- 不从公共请求接受 host、port 或 Runtime URL；
- 不注册公网 DNS；
- 不作为 `/api/v1` 服务。

### 13.2 loopback 与共享 token 比较

| 方案 | 优点 | 风险 | 结论 |
|---|---|---|---|
| 只依靠 loopback | 最简单 | 本机其他进程仍可能探测端口和构造请求 | 可运行但保护较弱 |
| loopback + 环境变量共享 token | 增加很小，实现简单，可阻止非预期本地调用 | 需要两个进程安全注入同一 secret | 推荐 |

MVP 已确认采用：

```text
X-ZTA35G-Runtime-Token: <环境变量注入的高熵 token>
```

规则：

- token 只来自两个进程的启动环境，不进入请求正文、数据库或日志；
- `live`、`ready` 和 `execute` 默认都必须携带 `X-ZTA35G-Runtime-Token`；
- Runtime 使用安全的常量时间比较；
- 缺失或错误 token 返回统一的内部安全错误，具体使用 401/403 或隐藏式 404 由阶段 1B 在不改变“默认都鉴权”的前提下统一；
- token 不是未来远程认证方案。

### 13.3 输入安全

- 只接受 `application/json`；
- 请求体最大 64 KiB；
- 限制 JSON 深度、字符串长度、数组长度和 diagnostics/warnings 数量；
- Tool ID 固定；
- 不允许任意路径、模块名、类名、函数名、环境变量名或 shell 参数；
- 不允许 pickle、joblib payload、Python object 或代码片段；
- 不允许客户端指定权重路径或设备文件；
- `.npy` 读取强制 `allow_pickle=False`；
- 响应体最大 4 MiB；
- Base64 和 `.npy` 在使用前完整校验。

### 13.4 日志安全

不记录：

```text
共享 token
完整图像 payload / Base64 / .npy bytes
Tensor 内容
完整用户对话
数据库或 MinIO credential
权重内容
完整 traceback 到 Backend 响应
```

Runtime 本地受限错误日志可以保存完整 traceback，但应避免记录完整图片、密钥和绝对敏感路径。

### 13.5 未来远程 GPU

一旦 Runtime 离开本机：

- 必须使用 TLS；
- 必须采用正式服务认证，优先 mTLS、短期服务令牌或等价服务身份；
- 共享环境变量 token 不能作为唯一远程安全措施；
- 需要网络 ACL、防重放、密钥轮换、证书和部署管理；
- 必须重新评估 Base64、响应大小和超时。

## 14. 结构化日志与排错

### 14.1 共同关联字段

Backend 和 Runtime 共同使用：

```text
request_id
task_id
tool_run_id
```

Runtime 不需要 actor_id、conversation_id 或 Idempotency-Key。

### 14.2 Runtime 事件

至少记录：

```text
runtime_started
model_loading
model_loaded
execution_received
sem_generation
mechanical_property_prediction
execution_completed
execution_failed
runtime_busy
runtime_stopping
```

每条日志至少允许：

```text
timestamp
level
event
request_id（适用时）
task_id（适用时）
tool_run_id（适用时）
runtime_contract_version
tool_id / tool_version / schema_version
device
duration_ms
error_code
safe_error_message
actual_runtime_parameters（受控字段）
model_bundle_id（适用时）
```

### 14.3 Backend Adapter 事件

建议记录：

```text
tool_client_request_started
tool_client_response_received
tool_client_transport_failed
tool_client_timeout
tool_client_response_invalid
tool_execution_output_mapped
```

Backend 后续继续记录：

```text
asset_persistence
result_persistence
explanation_generation
response_completed
```

### 14.4 故障定位表

| 故障位置 | 主要证据 |
|---|---|
| Backend 请求前 | 没有 `tool_client_request_started`；有输入/Registry/ToolRun 准备错误 |
| Client Adapter | 有 request_started，无 Runtime execution_received；Adapter 本地构造/认证/连接错误 |
| Runtime 传输 | Runtime 无请求或响应截断；Backend transport/protocol 错误 |
| 模型加载 | `model_loading` 后 `MODEL_LOAD_FAILED`；ready NOT_READY |
| SEM 生成 | `execution_received` 后 `sem_generation` 失败 |
| 性能预测 | SEM 成功，`mechanical_property_prediction` 失败 |
| 响应解析 | Runtime execution_completed，Backend `tool_client_response_invalid` |
| Asset 持久化 | Runtime 和 Adapter 成功，Backend `asset_persistence` 失败 |
| Result 持久化 | Asset AVAILABLE，但 `result_persistence` 失败 |
| Explanation | ToolResult 已提交，Explanation LLM 失败或超时 |

### 14.5 日志内容边界

记录耗时、安全错误码、设备、实际运行参数和主要步骤。不得记录完整图片 bytes、Tensor 内容、用户完整对话、权重内容或共享 token。完整 traceback 仅留在 Runtime 本地受限日志，不进入 Backend API 响应。

## 15. 未来远程 GPU 演进

### 15.1 保持不变

未来远程 GPU 不改变：

```text
Application
MaterialTool
Tool Registry / Tool Catalog
Task / ToolRun
ToolExecutionOutput
ToolResult
Asset / AssetService
NaturalLanguageExplanation
公共 /api/v1
```

### 15.2 需要变化

```text
Local ZTA35G Tool Client Adapter
→ Remote ZTA35G Tool Client Adapter 或配置

127.0.0.1
→ 受控远程服务地址
```

同时增加：

- TLS；
- 正式服务认证；
- 网络 ACL；
- 连接、读取和总超时；
- 服务部署、启动、健康和容量管理；
- 证书/密钥轮换；
- 远程日志和运维边界。

### 15.3 内部协议复用

`/internal/v1` 的 request/response/error 语义可以继续复用。Runtime 仍不注册为公共 Tool，不向前端暴露 URL，不访问平台数据库或 MinIO。

### 15.4 图像传输重新评估

当前 Base64 `.npy` 针对单张 512×512 本地图像。远程后根据真实网络和文件规模比较：

- 继续 Base64 `.npy`；
- multipart JSON + binary；
- 受控短期对象传输；
- 压缩数组格式。

任何调整都必须保持：Backend 能校验原始中立图像并继续负责正式 PNG；不能把 Runtime 内部 Tensor 或临时文件路径变成公共契约。

## 16. 与第一至第四节 A 的一致性

### 16.1 第一节

- 仍是一个 `zta35g_sem_virtual_lab` 复合 Tool。
- Application 仍只调用一次 `MaterialTool.execute`。
- Runtime 内部完成 SEM 生成和按需性能预测。
- Runtime 只是旧依赖兼容边界，不是通用微服务平台。
- 不引入 `model_version`、InferenceRun 或 StageRun。

### 16.2 第二节

- Runtime 使用本次生成的原始 SEM 完成性能预测。
- ToolExecutionOutput 尽可能一起返回图片、性能、completed/failed outputs 和 diagnostics。
- Backend 收到图像后先由 AssetService 正式编码并保存 PNG，再提交 ToolResult。
- 只请求性能时仍返回并保存 intermediate SEM。
- 可捕获性能失败时尽可能保留已生成 SEM。

### 16.3 第三节

- 平台持久化层级仍是 Task → ToolRun → Asset/ToolResult。
- Runtime 请求不创建新的数据库实体。
- actual_runtime_parameters、diagnostics 和 model_bundle_id 仍映射到 ToolRun/受控 provenance。
- 图片 bytes、Tensor 和 Runtime 内存对象不进入 PostgreSQL。
- Runtime 不承担 IdempotencyRecord。

### 16.4 第四节 A

- 公共三个写入 POST 在当前请求内同步等待稳定业务状态。
- Runtime 超时由 Backend 形成 ToolRun/Task FAILED 和公共 504。
- execute 不自动重试；公共显式重试创建新 tool_run_id 和 seed。
- 客户端断开不等于取消，结果不确定时复用公共 Idempotency-Key。
- Runtime 内部路径不进入 `/api/v1`。
- `/api/v1/health/ready` 只显示安全摘要，不暴露本节内部细节。

### 16.5 未发现的直接冲突

本节未发现必须修改第一、第二或第三节正文的真实直接冲突。`request_context` 在 MaterialTool 边界可以包含 conversation/actor 信息，但 Adapter 发送 Runtime 时主动裁剪这些字段，符合“不发送 actor_id、user_id 或完整 Conversation/Message”的最新决定。

## 17. 阶段 1B 待验证事项

以下是实测门槛，不是本轮实施计划：

1. Python 3.8 环境能否建立，旧依赖能否共存。
2. PyTorch/Torchvision/CUDA 与 RTX 4060 Laptop 驱动兼容。
3. NumPy、Joblib、Matplotlib、scikit-learn、SciPy、Pillow 实际版本。
4. DDPM、DenseNet121 和两个 SVR 权重加载。
5. DDPM missing/unexpected keys 及功能影响。
6. Runtime 启动与模型加载耗时。
7. 模型加载一次后多请求复用是否稳定。
8. 单次 SEM、性能和完整链路 P50/P95/P99。
9. 合理的 Backend connect/read/total timeout。
10. 超时后 Runtime 是否继续计算、持续 busy 或可以恢复。
11. CUDA/模型挂起和 Runtime 进程崩溃时的可观察行为。
12. 是否需要人工重启，以及最小操作手册。
13. 最大并发 1 时的显存峰值、内存峰值和 GPU 利用率。
14. `RUNTIME_BUSY` 立即拒绝的显存、耗时和用户体验；只有实测证明短有界等待有明显价值时才重新评估。
15. `num_samples=1`、`guide_scale=2.0`、`timesteps=1000` 这组固定配置能否正确运行，以及平台生成并记录的 seed 能否完整回显；不把它们提前开放为可调参数。若源码真实要求不同，先更新兼容性验收记录和设计基线，再修改配置。
16. 单张原始图像实际 dtype、shape、通道、C order、值域和有限值。
17. `.npy` `allow_pickle=False` 的编码/解码互操作。
18. Base64 `.npy` 实际响应大小、CPU 开销和 4 MiB 上限余量。
19. Backend 检查 NaN/Inf、shape、dtype、值域和 sha256。
20. 正式 PNG 量化/编码继续与第二节一致。
21. 可捕获性能失败时能否返回已生成 SEM 和 diagnostics。
22. Runtime error 到 Adapter/ToolExecutionOutput/公共错误的映射。
23. Runtime token 注入、比较、日志脱敏和错误行为。
24. live/ready 不触发完整推理，busy 表达清晰。
25. Backend 迟到响应丢弃和已失败 ToolRun 不被改写。
26. 客户端断开后 Backend 与 Runtime 的真实执行行为。

## 18. 验收场景

1. Runtime 绑定 `127.0.0.1`，未监听 `0.0.0.0` 或局域网地址。
2. 公共 `/api/v1` 不存在 Runtime execute、模型、上传或下载路径。
3. `live`、`ready`、`execute` 缺失或使用错误共享 token 时 Runtime 拒绝请求且不记录 token。
4. live 在不运行模型推理的情况下返回进程存活。
5. 模型未加载时 ready 为 NOT_READY；加载成功后为 READY。
6. Runtime busy 时 ready 显示 busy，第二个 execute 立即返回 `RUNTIME_BUSY`。
7. 启动后模型只加载一次，连续请求不重新加载权重。
8. 合法请求包含三个关联 ID、固定 Tool/版本、四维参数、requested outputs 和运行参数。
9. 合法请求固定携带 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000`；Runtime 逐项核对并回显，普通用户不能通过公共 API 修改，任何其他值即使落在某个数学合法范围内也被拒绝。
10. 请求携带 actor、MinIO、数据库、路径、模块名或任意 Python 参数时被拒绝。
11. 非 ZTA35G Tool 返回 `UNSUPPORTED_TOOL`。
12. schema/tool/runtime contract 不一致返回明确 mismatch 错误。
13. 只请求 SEM 时返回一张 `generated_sem` `.npy` 图像载荷。
14. 只请求性能时返回 intermediate SEM 和性能结果。
15. 同时请求两项时只生成并返回一张 SEM。
16. `.npy` 解码必须 `allow_pickle=False`，object dtype 被拒绝。
17. Backend 能检查 dtype、shape、NaN/Inf、值域、大小和可选 sha256。
18. Backend 而不是 Runtime 完成正式 PNG 量化、编码和 MinIO 保存。
19. 性能失败但 SEM 成功时按 requested outputs 正确形成部分成功或失败。
20. Runtime 返回 ID、版本或输出集合不一致时 Adapter 拒绝响应。
21. Runtime 崩溃或无响应时 ToolRun/Task 规范化失败，不伪造结构化成功。
22. execute 超时后 Backend 返回稳定失败，迟到响应不改写 ToolRun。
23. execute POST 不自动重试；`RUNTIME_BUSY.error.retryable=true` 只允许用户稍后显式重试，显式重试创建新 tool_run_id 和新 seed。
24. 公共 Idempotency-Key 重放不再次调用 Runtime。
25. Runtime 日志和 Backend 日志可用三个 ID 串联。
26. Runtime 响应和 Backend 公共响应不包含 traceback、权重路径、object key 或完整图片 payload。
27. 开发者手动启动 Runtime、确认 ready 后再启动 Backend；Backend 不拉起、重启或守护 Runtime。
28. 未来改成远程 Adapter 时 Application、MaterialTool、ToolResult、Task、Asset 和公共 API 无需改变。

## 19. 已形成的设计结论

```text
通信：127.0.0.1 本地 HTTP
路径：/internal/v1/health/live
      /internal/v1/health/ready
      /internal/v1/execute
图像：JSON + Base64 安全 NumPy .npy
dtype：float32 / <f4
shape：[512, 512]
值域：[-1.0, 1.0]
读取：allow_pickle=False
请求上限：64 KiB
响应上限：4 MiB
execute 自动重试：禁止
公共幂等：Backend/Application
模型加载：启动一次，多请求复用
最大并发：1
繁忙默认：立即 RUNTIME_BUSY
内部队列：无
正式 PNG：Backend/AssetService
默认安全：loopback + 环境变量共享高熵 token
未来远程：替换 Adapter，增加 TLS 与正式服务认证
```

## 20. 已确认设计决定

### 20.1 共享 Token

已确认：MVP 默认采用 `loopback 127.0.0.1 + 环境变量共享高熵 Token`，内部 Header 固定为 `X-ZTA35G-Runtime-Token`。`live`、`ready`、`execute` 默认都要求 Token；Token 只通过两个进程的启动环境注入，不进入数据库、请求正文或日志，Runtime 使用安全比较。未来远程 GPU 必须增加 TLS 和正式服务身份认证，不能把本地共享 Token 当作正式远程认证方案。

### 20.2 阶段 1B 初次启动方式

已确认：开发者手动启动 `materialsagent-zta35g` Runtime，检查 `/internal/v1/health/ready`，再启动 `materialsagent-backend`。Backend 不自动创建 Conda 子进程，不自动启动或重启 Runtime，不充当守护进程，也不管理 Runtime 生命周期；未来部署需要自动启动时另行设计。

### 20.3 繁忙策略

已确认：MVP 最大 execute 并发为 1，Runtime 内部无队列，繁忙时立即返回 `RUNTIME_BUSY` 且 `error.retryable=true`。`retryable` 只允许用户稍后显式重试；Backend 不自动重试 execute，显式重试必须创建新的 `tool_run_id` 和 seed。只有阶段 1B 实测证明短有界等待有明显价值时才重新评估，始终禁止无界排队。

上述决定不增加公共 Runtime URL、Runtime 业务幂等、execute 自动重试、Runtime 直传 MinIO 或 Runtime 正式 PNG 编码责任。第四节 B 已通过项目负责人复核，确认为阶段 0 设计基线。
