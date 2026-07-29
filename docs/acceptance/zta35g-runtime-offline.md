# P1B1 ZTA35G Runtime 离线验收记录

## 工作包目标

在不创建真实模型环境、不安装模型依赖、不打开四份真实权重、不使用 GPU 的前提下，实现 Python 3.8 兼容的真实 ZTA35G Runtime 源码边界，并使用 Fake Loader、Fake Model Components 和真实 loopback HTTP 服务验证与现有 Backend Adapter 的内部契约。

本记录不把 Fake Engine 结果表述为真实模型兼容性或真实模型推理证据。

## 最终验收状态

- P1B1：`P1B1_PROJECT_OWNER_ACCEPTED`
- 最终代码审查：`APPROVED`
- 项目负责人验收：`APPROVED`
- 唯一条件性本地提交：`AUTHORIZED`
- 真实权重打开次数：`0`
- 真实模型调用次数：`0`
- GPU 调用次数：`0`
- P1B2：`NOT STARTED / NOT AUTHORIZED`

## Git 基线

- 分支：`main`
- HEAD：`61a6a88db674cbd84990d6b5288d8db8eec480ed`
- 提交主题：`test: complete real DeepSeek provider acceptance`
- 父提交：`ffd29353cb4682c74fd3455425822999444bb1d2`
- 开始时工作区、暂存区和未跟踪文件：均为空

## 实际文件范围

- 治理：阶段 1 计划、当前进度、`P1B1` scope allowlist、支持
  `M12A | P1B1` 参数的阶段 1A Runner
- Runtime 生产源码：`constants.py`、`contracts.py`、`config.py`、`model_architecture.py`、`model_bundle.py`、`inference.py`、`app.py`、`main.py`
- Runtime 测试：unit、contract、共享 compatibility 默认授权门
- Backend：仅新增 `test_real_runtime_adapter.py`
- 未修改 Backend 生产代码、Mock Runtime、Frontend、五份设计基线、migration 和 `SEM/`
- 第二轮审查修订只改动既有 P1B1 路径中的 Runtime
  `app.py`、`contracts.py`、`inference.py`，对应 contract/unit/compatibility
  测试，以及本记录和当前进度；未改动阶段 1 计划、Phase 1A Runner 或 scope 脚本。

## 离线架构

```text
RuntimeSettings
  -> startup single load
  -> ModelBundleLoader
       -> all four size and SHA-256 validations
       -> delayed object loader
  -> ZTA35GInferenceEngine
       -> one SEM generation per request
       -> optional same-image mechanical chain
       -> strict image/performance validation
  -> ThreadingHTTPServer on 127.0.0.1
       -> token on live/ready/execute
       -> one nonblocking execution slot
       -> bounded strict JSON response
  -> existing LocalZTA35GToolClientAdapter
```

模型结构模块在普通 import 时不创建 Tensor、不选择 device、不加载权重。真实 `torch`、`torchvision`、`joblib` 和 `numpy` 对象只在 P1B2 明确调用真实组件的 `load()` 或执行路径时导入。

## 原始代码适配差异

- 未直接调用 `VirtualLab.run()`：原函数包含单例、打印、Matplotlib、目录创建和 PNG 写入，且不能按 `requested_outputs` 跳过不需要的性能链。
- 固定 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000`；没有沿用原始默认 `num_samples=3`。
- seed 由请求显式传入；真实组件使用本次独立 `torch.Generator` 控制初始噪声和每个扩散步骤，不使用 Python `hash()`，不依赖或改写全局 RNG。
- Runtime 不创建 `Generated_Results`，不调用 Matplotlib，不写 PNG；只在内存生成 Base64 `.npy`。
- 新模型定义中所有会进入 DDPM `state_dict` 的模块属性名与只读原文件一致：
  `cond_mlp`、`conv0`、`mid_block1`、`mid_attn`、`mid_block2`、
  `bnorm1`、`bnorm2`、`relu`、`mha`、`ln`、`ff_self` 和 `op`；
  没有用大规模 key rename 表掩盖模型定义差异。
- DDPM 保留 `strict=False` 只用于取得 mismatch 计数；missing 或 unexpected
  任一非零立即 fail closed。DenseNet 采用 `strict=True`。错误只保留安全摘要，
  不返回私有键名或绝对路径。
- 输出图像必须为二维 C-contiguous `<f4`、`512×512`、有限值且位于
  `[-1,1]`。每个 DDPM 迭代结束检测到 NaN/Inf 时立即抛
  `InvalidModelOutputError`；生产代码不再包含 `torch.nan_to_num` 或
  `np.nan_to_num`，不会继续 DenseNet/SVR 或静默修复为成功。
- 只请求 `sem_image` 时不调用 DenseNet/SVR；只请求性能时返回同一次生成的 intermediate SEM；同时请求时只生成一次并复用。
- DenseNet 旧键转换、DDPM `ema`/`model`/raw 选择及 `module.`/`model.` 前缀清理由独立纯函数覆盖。

## Fake Engine 合同

- 三种 `requested_outputs` 均使用真实 `127.0.0.1` 随机空闲端口验证。
- 模型启动加载一次，多请求复用同一 Engine。
- `live`、`ready`、`execute` 均要求 `X-ZTA35G-Runtime-Token`。
- busy 时 ready 仍为 Runtime `READY`，但 `busy=true`、`can_accept_execution=false`；现有 Backend Adapter 映射为 `DEGRADED`。
- 加载成功空闲时 ready 为 HTTP 200、`AVAILABLE`、`model_loaded=true`、
  实际 `device.kind`、`can_accept_execution=true`；加载失败时为 HTTP 503、
  `NOT_READY`、`model_files.status=INVALID`、device unavailable/unknown、
  `model_bundle_id=null` 和 `MODEL_LOAD_FAILED`。未使用未定义的
  `model_files.status=UNAVAILABLE`。
- 第二个 execute 立即返回 `503 RUNTIME_BUSY`，没有排队或自动重试。
- 关闭先禁止新执行，再等待当前 execute 释放执行锁；活动请求形成稳定响应后才关闭
  Engine 和 HTTP Server。关闭期间的新请求安全返回 503，且不启动模型、不增加执行计数；
  重复关闭保持幂等。
- Runtime 只接受固定 `MODEL_BUNDLE_ID="zta35g-sem-original-bundle"`。启动后若
  `is_loaded()`、bundle 身份或 cpu/cuda device 任一不合法，立即且仅一次关闭 Engine，
  ready 保持 503；execute response 中任意其他 bundle 身份映射为安全 500。
- 性能链失败可形成 `PARTIALLY_SUCCEEDED`；SEM 失败不会执行性能预测。
- 请求和响应分别执行 64 KiB、4 MiB 门；出站状态、分区、error、diagnostics、warning、图片 hash/base64、性能单位和 bundle id 均严格校验。
- Runtime 使用标准库 `logging` 写安全 JSON 事件；独立入口配置专用 INFO
  StreamHandler，正常启动无需外部 logging 配置即可记录启动、加载、加载失败、
  接收、busy、SEM、性能、完成、执行失败和停止。只记录白名单字段；Token、
  请求正文、Base64、Tensor、权重路径和完整异常正文均未进入日志。

## 懒加载与未授权操作证据

- 干净 Python 子进程隔离导入结果：`HEAVY_IMPORTS=[]`
- 四份真实权重打开次数：`0`
- `torch.load` 真实路径调用次数：`0`
- `joblib.load` 真实路径调用次数：`0`
- 真实模型调用次数：`0`
- GPU 调用次数：`0`
- DeepSeek 调用次数：`0`
- 新 Python/Conda/模型依赖安装次数：`0`
- `npm ci` 仅恢复既有 lockfile 所锁定的 Frontend 依赖；`package.json` 和 `package-lock.json` 未修改

## 测试结果

- 第二轮审查聚焦五文件：`110 passed, 1 skipped`
- Runtime 全量：`126 passed, 3 skipped`
- Runtime compatibility 默认门：`4 passed, 3 skipped`。普通授权矩阵额外要求
  实际解释器精确为 Python 3.8；三条真实测试共同要求
  `ZTA35G_REAL_MODEL_ACCEPTANCE=P1B2_PROJECT_OWNER_AUTHORIZED`、
  `CONDA_DEFAULT_ENV=materialsagent-zta35g` 和非空 `ZTA35G_MODEL_ROOT`；
  两条真实推理/资源测试还要求
  `ZTA35G_GPU_ACCEPTANCE=P1B2_GPU_AUTHORIZED`。若普通门均通过且已授权 GPU，
  `torch.cuda.is_available()` 不为 true 时测试明确失败，不得 skip 或回退 CPU。
- Backend Adapter 黑盒：`7 passed`
- Mock Runtime：`11 passed`
- 官方 Phase 1A Runner 内的 M11 E2E：`24 passed`
- 官方 Phase 1A Runner 内的 Backend 全量：`985 passed`
- Frontend：`9 files / 204 tests passed`
- Frontend typecheck：通过
- Frontend build：通过
- `pip check`：通过
- `compileall`：通过
- `SEM_INTEGRITY_OK`
- `SCOPE_OK P1B1`
- 正式 Phase 1A Runner：
  `20260729T163241Z-d7841be818ab`、
  `scope_milestone=P1B1`、
  `PHASE_1A_AUTOMATION_PASSED`、`failed=0`

## 第一轮代码审查修订 RED / GREEN

- 模型身份与 mismatch RED：原模型属性名测试失败；missing、unexpected 和两者
  同时非零三型均未抛错。GREEN：对应 `6 passed`，含 zero mismatch 成功、
  三型 fail closed、当前和已加载对象关闭以及安全错误摘要。
- 架构证据复审 RED：原测试只做必要属性子集判断，且实现缺少原定义的
  `ConditionEmbeddings.emb_dim`。GREEN：精确属性集合、默认参数以及完整
  `__init__`/`forward` AST 结构摘要锁定所有层构造、通道/卷积参数和数学顺序；
  逐迭代 AST 门同时确认 NaN/Inf 检查是采样循环体最后一步并立即抛错，
  对应聚焦 `2 passed`。
- DenseNet strict mutation RED：临时把实现改回 `strict=False` 后，严格加载测试
  以 `[False] != [True]` 失败。恢复后 DDPM non-strict 汇总与 DenseNet strict
  两项 `2 passed`。
- NaN/Inf 与加载清理 RED：生产源码仍命中 `torch.nan_to_num`；四类加载失败会
  二次调用 Loader，DDPM/DenseNet/device/beta 中途失败未关闭 bundle。
  GREEN：`7 passed`，包含 `INVALID_MODEL_OUTPUT` 映射、性能调用 0、无图片/数据、
  四类清理、禁止重复 load 和干净子进程懒导入。
- ready 与日志 RED：成功 device 为 unknown、失败资源未立即关闭、失败 ready
  仍为 HTTP 200、日志事件为空。GREEN：Runtime/Backend 聚焦 `7 passed`，
  覆盖 idle、busy/DEGRADED、503 NOT_READY、正常/busy/load failure/execute
  failure 安全日志。
- 独立入口日志复审 RED：干净 Python 子进程导入
  `configure_runtime_logging` 失败，说明先前日志只在测试临时提升 INFO 门槛时
  可见。GREEN：入口配置幂等专用 INFO handler；干净子进程与正常/失败日志
  三项 `3 passed`。
- 次要契约 RED：`seed=2**63` 被接受；深层 JSON 泄漏 `RecursionError`。
  GREEN：契约聚焦 `23 passed`。
- compatibility RED：三个共享授权 fixture 均不存在，4 个 setup error。
  GREEN：纯授权矩阵 `1 passed`，三条真实测试在未设置授权变量时明确
  `3 skipped`，且跳过发生在任何 Torch/Joblib/权重 import 之前。
- Runner RED：`-ScopeMilestone P1B1` 在参数绑定阶段报
  `NamedParameterNotFound`。GREEN：本轮只读参数结构检查确认默认仍为 M12A、
  ValidateSet 精确为 M12A/P1B1；正式 P1B1 Runner pre/post scope 命令与日志
  均表达 P1B1，最终 `failed=0`。
- 修订后只读复核未发现剩余 Critical、Important 或 Minor，assessment 为
  `APPROVED`；该结论仅覆盖本次 P1B1 代码复核，不替代项目负责人验收，也不授权
  进入 P1B2。

## 第二轮代码审查修订 RED / GREEN

- 关闭一致性：RED 为 `2 failed`，分别暴露活动 execute 完成前组件已被关闭、关闭
  状态下新请求仍返回 200；GREEN 为两项聚焦 `2 passed`。真实 loopback HTTP
  阻塞测试证明 close 在释放 execute 前未返回且 close count 为 0，释放后原响应稳定、
  close count 为 1、请求与关闭线程均退出；关闭状态新请求为安全 503、执行计数不增。
- Python 版本与 GPU 授权门：RED 为 `4 failed, 1 skipped`，暴露授权函数没有实际
  Python 版本参数且 CUDA false 未失败；GREEN 为授权聚焦
  `4 passed, 1 skipped`、compatibility 全量 `4 passed, 3 skipped`。Python 3.11、
  3.9 及“环境名正确但版本错误”均得到 `PYTHON_VERSION` 门失败；GPU 授权后的
  CUDA 不可用明确 fail，并发生在 Loader/推理前。两条 GPU 测试还断言加载后
  `engine.device_kind == "cuda"`。
- 固定 bundle 身份：RED 为 `6 failed, 7 passed`，暴露三种启动不变量失败未关闭、
  execute response 接受其他 bundle，以及 Engine 用默认值遮蔽空身份；GREEN 为
  `13 passed`。正确固定 bundle 的 CPU/CUDA 启动成功；错误 bundle、unknown device
  和未 loaded 均关闭一次并 ready 503；空、其他、带空白和超长 response bundle 均为
  安全 `INTERNAL_RUNTIME_ERROR`。只读复核继续发现运行期错误 bundle 会在 response
  校验前进入诊断日志；新增真实 HTTP canary 后 RED 为 `1 failed, 1 passed`，改为先
  完整校验 response、再只记录固定 bundle/受限 device 后 GREEN 为 `2 passed`，
  日志不再包含错误身份或私有异常正文。
- RecursionError 稳定性：新增 monkeypatch 确定性测试初次通过；临时移除生产捕获后
  RED 为 `1 failed` 且原始 `RecursionError` 逸出，恢复映射后 GREEN 为
  `1 passed`，固定断言 HTTP 400、`INVALID_RUNTIME_REQUEST` 且不泄漏原异常正文。
- 上述修订后的聚焦组合为 `110 passed, 1 skipped`，Runtime 全量为
  `126 passed, 3 skipped`。干净隔离 compatibility 进程为
  `4 passed, 3 skipped`、`HEAVY_IMPORTS=[]`；四份真实权重、真实 Torch/Joblib
  loader、真实模型、GPU 和 DeepSeek 调用均为 0。
- 第二轮正式 Phase 1A Runner
  `20260729T163241Z-d7841be818ab` 为
  `PHASE_1A_AUTOMATION_PASSED`、`failed=0`：M11 E2E `24 passed`、Backend
  `985 passed`、Mock Runtime `11 passed`、Frontend 9 files / `204 passed`，
  typecheck/build、pip、compileall、pre/post P1B1 scope、SEM、Git 和动态安全扫描
  均通过；结束后 3000/8000/8100 无监听、Compose 运行服务为 0、运行状态文件已删除。
- 日志 canary 与关闭线程断言修订后的只读复核为 Critical 0、Important 0、
  Minor 0、`APPROVED`；项目负责人最终验收为 `APPROVED`，并授权唯一条件性本地提交。
  该授权不授权进入 P1B2。

## 已知限制

- Python 3.8 兼容性当前由 `ast.parse(..., feature_version=(3, 8))`、禁用语法扫描
  和真实验收前的实际解释器门证明；尚未创建或运行 Python 3.8 模型环境，因此不能
  表述为真实模型兼容性通过。
- 默认 compatibility 测试是授权门，不是四权重加载、真实推理、GPU 或资源测量通过证据。
- Phase 1A Runner 默认不传参时仍使用历史 `M12A` scope；P1B1 只通过显式
  `-ScopeMilestone P1B1` 使用新 allowlist。历史 M12A allowlist 未修改。
- 第二轮直接在无受控集成栈的 shell 运行 Backend 全量约 4 分钟仍无测试断言输出；
  仅在核对命令与 ownership 后清理该命令精确的 Conda/pytest PID 树。权威 Backend
  全量证据来自正式 Runner 启动的受控栈，为 `985 passed`。
- `npm ci` 报告既有 lockfile 中 6 个 high severity audit 项；本工作包禁止修改 Frontend 依赖，未执行 `npm audit fix`。

## P1B2 暂停点

P1B2 仍为 `NOT STARTED / NOT AUTHORIZED`。P1B1 已由项目负责人验收，并仅授权本工作包的唯一条件性本地提交；在项目负责人另行授权 P1B2 前，不得创建 `materialsagent-zta35g` 环境、安装 Torch/CUDA/scikit-learn、打开真实权重、运行真实 DDPM、使用 GPU、启动真实 Runtime、执行真实浏览器模型验收或真实 DeepSeek + Runtime E2E。
