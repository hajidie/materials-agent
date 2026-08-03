# Phase 1B 验收报告

## 一、恢复基线

- 报告初始工作单元：P1B2 门一——独立环境与依赖验证。
- 当前工作单元：P1B2 门四 Stage C 安全整改验收提交。
- 开始分支：`main`。
- 开始 HEAD：`7367b410f800b63efa9b9d4ba94095a3a45efb7a`。
- 开始提交主题：`feat: add offline zta35g runtime`。
- 开始 working tree 干净、staging empty、untracked 为 0；本轮未执行 reset、
  restore、stash、clean、switch 或 checkout。

## 二、实际硬件

- 实际 GPU：NVIDIA GeForce RTX 3060 Laptop GPU。
- 显存：`6144 MiB`。
- Compute Capability：`8.6`。
- 最终只读查询的驱动版本：`595.79`。
- 该硬件并非旧候选 RTX 4060 / 8 GB；后续真实模型计划必须以 RTX 3060 Laptop
  GPU / 6144 MiB 为事实基线。

## 三、环境创建与人工恢复

- 正式门一审计环境：`materialsagent-zta35g`，Python `3.8.20`。
- 项目负责人在首轮安装阻塞后手工完成 GPU 版 PyTorch 和其他候选依赖安装，并
  保留该独立环境供只读复核。
- Codex 本轮未执行 Conda/Pip 创建、更新、删除、安装、卸载或升级；Backend 和
  Base 环境以及 Conda 配置均只读保护。
- Backend 回归解释器为 Python `3.11.15`。
- 结束审计中 Backend 87 包指纹与开始值一致。Base 原始开始／结束包数组快照没有
  持久化，因而无法逐字段复原旧哈希差异；日志证明 `d53…` 与 `d446…` 来自不同的
  数组处理和 JSON 序列化算法，而同一算法下的开始／结束 `d446…` 比较结果一致。
- Base 当前 Conda 语义投影连续三轮均为
  `7e04c35067f4d257351063091a2d64a79d75c9497f08bf6ff7f008782a3b6d70`，
  包数量均为 505；pip 语义投影连续三轮均为
  `69bb3db228283bee065c030f3060c8200dcc2fb20c8879d9e32342d14ab9a937`，
  包数量均为 437。
- Base 最后 Conda revision 为 `2025-11-14`，本轮无新 revision；
  `conda-meta/history` 和 package JSON 本轮均无写入，也没有包名、版本、build、
  channel、subdir 或 pip 包身份变化证据。结论为 `BASE_PACKAGE_SET_UNCHANGED` /
  `AUDIT_FINGERPRINT_FALSE_POSITIVE`：此前阻塞来自跨实现比较不兼容的审计指纹，
  属于审计方法误报。

## 四、实际依赖

| 依赖事实 | 实际版本或结果 |
|---|---|
| Python | `3.8.20` |
| torch | `1.13.1+cu116` |
| torchvision | `0.14.1+cu116` |
| CUDA Runtime | `11.6` |
| numpy | `1.22.3` |
| scipy | `1.10.1` |
| joblib | `1.4.2` |
| scikit-learn | `1.0.2` |
| Pillow | `10.4.0` |
| pytest | `8.3.5` |
| pip check | `No broken requirements found.` |

只读核对同时确认 `torch.cuda.is_available() == True`、设备数为 1、GPU 名称和
Compute Capability 与第二节一致。

## 五、CUDA 基础验证

项目负责人在人工恢复后取得的极小 CUDA Tensor 证据为：

```text
result: [2.0, 3.0]
allocated: 1024 bytes
reserved: 2097152 bytes
CUDA_BASIC_TEST_PASSED
```

Codex 本轮不重复创建 CUDA Tensor。该证据只说明当前候选 PyTorch/CUDA/GPU
组合可完成基础 Tensor 运算，不代表真实权重加载、DDPM、DenseNet、SVR 或完整
Runtime 验收。

## 六、Python 3.8 Runtime 离线测试

- RED：目标 Python 3.8 修改前为 `124 passed, 3 skipped, 2 failed`。
  `test_inference.py` 因 Python 3.8 没有 `ast.unparse` 失败；
  `test_model_bundle.py` 因 `ast.dump` 内部表示随解释器变化导致固定 SHA-256
  不一致。代码审查确认两项均为测试兼容性问题，生产 Runtime 无需修改。
- 修订一：以 AST 节点类型和字段锁定每个采样步骤末尾重新赋值 `values`，随后按
  顺序检查 `torch.isnan(values).any()` 和 `torch.isinf(values).any()`，任一命中
  立即无参数抛出 `InvalidModelOutputError`；没有降级为字符串查找。
- 修订二：以标准库 `tokenize` 生成忽略纯格式 token 的语义摘要。Python 3.8 与
  Python 3.11 对 7 个类的 `__init__` / `forward` 共 14 个方法生成完全相同摘要后，
  才更新共同期望值；self 属性精确集合、禁止属性、默认参数和方法结构保护均保留。
- GREEN 聚焦：Python 3.8 为 `44 passed`；Python 3.11 为 `44 passed`。
- GREEN 全量：Python 3.8 为 `126 passed, 3 skipped`；Python 3.11 为
  `126 passed, 3 skipped`。两边均 0 failed，unit 和 contract 全部通过。
- 精确三项 compatibility skip 为 `test_minimal_inference.py`、
  `test_model_loading.py` 和 `test_payload_and_resources.py`；原因均明确为 P1B2
  真实模型授权门缺失，不是依赖导入、路径、收集或生产代码错误。
- 两套解释器执行 `compileall -q zta35g-runtime/src` 均退出 0。

## 七、依赖锁与环境文件

- `zta35g-runtime/requirements-win-py38.lock.txt` 从目标环境
  `pip freeze --all` 生成；`pip==24.2` 保持可移植版本 pin，torch 和 torchvision
  的本机 wheel 路径则规范化为精确官方 HTTPS wheel direct references，并分别
  固定 SHA-256
  `1c33942d411d4dee25e56755cfd09538f53a497a6f0453d54ce96a5ca341627b`
  与
  `fefa6bee4c3019723320c6e554e400d6781ccecce99c1772a165efd0696b3462`。
- 使用 `packaging.requirements.Requirement` 校验两个 direct references 的名称、
  官方 URL 和 SHA-256；同时使用目标环境 `importlib.metadata` 确认 torch 为
  `1.13.1+cu116`、torchvision 为 `0.14.1+cu116`，其余 22 个发行版名称和版本
  继续与当前环境一致。
- `environments/materialsagent-zta35g.yml` 来自实时 Conda
  `env export --no-builds`；仅删除 `prefix:`，保留实际 Conda/Pip 包集合并使用
  与锁文件相同的带哈希官方 direct references。
- YAML 与规范化实时导出逐行一致；两个文件均为 UTF-8/LF，不含 `file:///`、
  本机绝对路径、editable 或 `-e` 记录。
- 环境记录已包含精确官方 wheel 来源和 SHA-256；本轮没有删除或从零重建现有
  环境，也没有安装、卸载或更新包。

## 八、安全边界

- 本轮精确变更范围为 8 个 P1B2 allowlist 路径：两个测试、阶段计划、当前进度、
  本报告、Scope 脚本、Conda 环境文件和依赖锁。
- 真实 `torch.load` 权重调用：0。
- 真实 `joblib.load` 权重调用：0。
- 真实模型构造：0。
- DDPM 采样：0。
- GPU 模型推理：0。
- 真实 Runtime：未启动。
- Backend、MinIO、DeepSeek：未调用。
- 根 `.env`：未读取、未修改。
- Runtime 生产源码、Backend、Frontend、Mock Runtime、`SEM/`、migration、
  阶段 1A Runner 和五份设计基线均未修改。
- `SEM_INTEGRITY_OK` 证明清单哈希保持一致；这一检查会读取文件内容计算哈希，
  因而不表述为操作系统层面的“权重零读取”。本轮准确边界是没有调用真实
  反序列化或模型加载接口。

## 九、门一收尾时已知限制

- 当前只验收独立环境、候选依赖、CUDA 基础可用性和 Runtime 离线测试。
- 三个 compatibility 测试仍由真实授权门明确跳过；四份权重尚未通过真实加载
  兼容性检查。
- 未验证真实 DDPM/DenseNet/SVR 构造、最小 GPU 推理、资源峰值、真实 Runtime
  生命周期、Backend/MinIO 接入或浏览器 E2E。
- RTX 3060 Laptop GPU 的 6144 MiB 显存可能约束后续固定参数推理；必须在独立
  授权门中测量，不能由 CUDA 基础 Tensor 结果推断。
- 原始审计未保留开始／结束包数组快照，无法逐字段复原旧哈希差异；后续已通过命令
  日志、稳定语义投影、revision 和元数据时间确认该差异为审计算法误报。

## 十、门一收尾时下一授权门

- P1B2 门一：`COMPLETE / PROJECT_OWNER_ACCEPTED`。
- P1B2 门二：`NOT STARTED / NOT AUTHORIZED`。
- 门一收尾时尚未到达或执行权重加载授权点；当时 P1B2 门二仍需项目负责人另行明确授权。
- 未经新的明确授权，不得设置真实授权变量、打开权重、调用真实 loader、构造
  模型、执行 DDPM、启动真实 Runtime，或调用 Backend、MinIO、DeepSeek。
- 本轮不执行 git add、commit、push 或 amend。

最终状态：`P1B2_ENVIRONMENT_GATE_PROJECT_OWNER_ACCEPTED`。

## 十一、P1B2 门二真实权重加载兼容性

### 11.1 恢复、资源与文件身份

- 门二恢复基线为
  `main@c86c8eddfbb7a4b7354dd2299465cf352530623a`，开始时精确保留
  3 个已审阅门二路径、暂存区为空、未跟踪文件为 0。
- 主机总物理内存为 `15.40 GiB`；最终完整加载前外部资源门空闲
  `8.28 GiB`。测试进程进入加载监测时空闲 `8572153856` bytes，观测最低空闲
  `6031679488` bytes，观测峰值已用 `10507776000` bytes，完整 bundle
  释放后空闲 `8514912256` bytes。
- GPU 为 NVIDIA GeForce RTX 3060 Laptop GPU / `6144 MiB`；加载前后显存使用
  均为 `0 MiB`，计算进程为 0。未设置 `ZTA35G_GPU_ACCEPTANCE`。
- 前置 `SEM_INTEGRITY_OK` 为 57 文件、`2043071133` bytes、aggregate fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- 四个固定文件均为 manifest 唯一项，身份如下：

| 相对路径 | 大小（bytes） | SHA-256 |
|---|---:|---|
| `SEM/ZTA35G_lab/ddpm_512_epoch_800.pth` | 1999585053 | `a9c6b369e05a6a330b4ab4587060a51c909c350d58fef8e1dcfcf3937a879dd3` |
| `SEM/ZTA35G_lab/densenet121-a639ec97.pth` | 32342954 | `a639ec97d7c33b07ae66f0b5fb7d0192f95a3b11b7576c66c0126c2a727c4395` |
| `SEM/ZTA35G_lab/SVR_model/Final_Yield_Strength.pkl` | 171661 | `547c4794329756dcb979518fab921abf599a307ae304bef9bb42e90f3d5945d3` |
| `SEM/ZTA35G_lab/SVR_model/Final_Elongation.pkl` | 14061 | `debe460fb230a4c5a64dcd8a1882478ef373c71619b9efe580d5e2f7008c6c53` |

### 11.2 DDPM 与 DenseNet

- DDPM checkpoint 顶层为 mapping，同时存在受控 `ema` / `model` 候选，
  不存在 `state_dict` 候选；正式优先级选择 `ema`。checkpoint 与
  ConditionalUNet 均为 271 个 key，全部 271 项为 Tensor，missing、
  unexpected、shape mismatch 和 dtype mismatch 均为 0。
- DDPM 参数为 271 个 Tensor、`124946629` 个元素；buffer 为 0。分阶段加载耗时
  `1.060 s`，加载后保持 CPU，随后释放。
- DenseNet checkpoint 经正式 legacy key 转换后为 606 个 key；当前
  DenseNet121 为 727 个 key。预比较 missing 121、unexpected 0、
  shape mismatch 0、dtype mismatch 0。
- 121 个 missing 精确且全部为 121 个启用 running stats 的 BatchNorm 模块的
  `num_batches_tracked` counter 集合；不存在 weight、bias、running_mean、
  running_var、classifier 或卷积参数缺失。
- 使用全新 DenseNet121 和独立 `OrderedDict` 副本真正执行
  `load_state_dict(..., strict=True)`；返回 missing 0、unexpected 0。
  PyTorch 1.13.1 内置版本兼容路径在其内部加载副本中精确加入 121 个 counter；
  调用方 state 副本保持不变。模型中的全部 counter 均为 CPU `int64` 标量 0，
  checkpoint 原有 Tensor shape 全部精确加载。
- 该结论为
  `DENSENET_LEGACY_BATCHNORM_COMPATIBILITY_CONFIRMED`，不是忽略 missing key。
  没有手工填充模型参数，没有使用 `strict=False` 接受 DenseNet，生产
  `ModelBundleLoader` 继续使用 `strict=True`，生产源码未修改。
- DenseNet 参数为 364 个 Tensor、`7978856` 个元素；buffer 为 363 个 Tensor、
  `83769` 个元素；分阶段加载耗时 `0.157 s`。

### 11.3 两个 SVR 与完整 bundle

- 两个顶层对象均为
  `sklearn.compose._target.TransformedTargetRegressor`，最终 estimator 均为
  `sklearn.svm._classes.SVR`；关键对象全部来自受控 sklearn/numpy/joblib 模块。
- Yield Strength 外层 Pipeline steps 为 `prep`、`svr`；其图像分支内层
  Pipeline steps 为 `scaler`、`pca`，PCA `n_components_=5`。
  顶层 `n_features_in_=3076`，support vectors shape 为 `[23, 9]`。
- Elongation 外层 Pipeline steps 为 `prep`、`svr`，无 PCA；
  顶层 `n_features_in_=3076`，support vectors shape 为 `[21, 8]`。
- 上述顶层类型、最终 estimator、外层及全部 Pipeline steps、输入维度及其来源、
  PCA components 和 support vectors shape 均由固定安全期望逐字段硬断言通过，
  两个模型的安全摘要均为 `exact_identity=true`，不是观察性输出。
- 两者 3076 维输入契约与正式链路一致：4 个工艺参数加 DenseNet
  average/maximum/minimum 三组各 1024 维图像特征。分阶段 SVR 身份检查代码未
  显式调用 predict、transform、fit 或 score。
- 正式 `ModelBundleLoader(model_root).load()` 在 CPU 成功加载
  `zta35g-sem-original-bundle`，四个 load summary 均为 missing 0 /
  unexpected 0；完整 bundle 加载耗时 `2.673 s`。
- `ModelBundleLoader` 是无状态工厂，不提供 `is_loaded()` API，也没有把不存在的
  `is_loaded()` 作为验收证据。为遵守不得构造第二套完整模型的边界，未重复调用
  `load()`；`second_full_load_executed=false`。`LoadedModelBundle._closed`
  在 close 前后为 `false → true`，owner 引用释放后 bundle 与四对象弱引用均为空。
- 分阶段验证加正式完整 bundle 的受控反序列化次数为每个固定文件 2 次。正式完整
  bundle 加载路径由运行期 canary 保护，SVR/Pipeline predict、PCA/其他
  transform、fit 和 score 调用计数均为 0。整个真实加载测试的模型 forward、
  DDPM sample/采样循环、Module/Tensor CUDA 和 Module/Tensor `to("cuda")`
  计数均为 0。
- forward 保护包含两层：`torch.nn.Module.__call__` 的 module-call canary，以及
  DDPM 每个动态 ConditionalUNet 类、`torchvision.models.DenseNet.forward` 和
  `torch.nn.Sequential.forward` 的 direct-forward canary；最终计数均为 0。

### 11.4 测试、范围与状态

- DenseNet 精确集合规则以纯 Python 小型 fake state 覆盖：完整 counter 集合允许
  进入 strict load；少一个预期 counter、多一个伪造 counter、缺 running_mean、
  unexpected key、shape mismatch 和 strict 异常均 fail closed。
- 代码审查修订的首轮 RED 为 `9 failed, 7 passed, 6 deselected`；增加精确身份
  helper 和双层 forward canary 后，目标及 Backend 解释器的指定聚焦组均为
  `16 passed, 6 deselected`。SVR fit/score 类收集另经 `1 failed → 1 passed`
  验证。
- SVR 负例不打开权重，覆盖错误顶层类型、错误最终 estimator、错误 Pipeline、
  错误 PCA、错误 support vectors shape，以及“3076 正确但类型错误”仍拒绝。
- 代码审查修订后最终授权的 `test_model_loading.py`：
  `22 passed in 8.30s`。
- 清除授权后，目标 Python 3.8 Runtime：`143 passed, 3 skipped`；
  Backend Python 3.11 Runtime：`143 passed, 3 skipped`。精确三项 skip 仍为
  未授权的真实 model-loading、minimal inference 和 payload/resources 测试。
- 两套解释器的 `compileall -q zta35g-runtime/src` 均退出 0。
- Runtime 生产源码、Backend、Frontend、Mock Runtime、环境/锁文件、`SEM/`、
  migration 和五份设计基线均未修改；未启动真实 Runtime，未执行任何推理，
  未调用 Backend、MinIO 或 DeepSeek。
- P1B2 门二状态：
  `COMPLETE / PROJECT_OWNER_ACCEPTED`。门三、门四保持
  `NOT STARTED / NOT AUTHORIZED`；门二只允许本次唯一验收提交，不执行 push
  或 amend。

门二最终状态：
`P1B2_MODEL_LOADING_GATE_PROJECT_OWNER_ACCEPTED`。

## 十二、P1B2 门三真实 GPU 最小推理与资源测量

### 12.1 恢复基线、授权与资源门

- 恢复基线精确为
  `main@62e0273ff32bd1a7462abf2e9f33d898b993eb43`，subject
  `test: verify zta35g model loading compatibility`，parent
  `c86c8eddfbb7a4b7354dd2299465cf352530623a`；开始工作区干净、staging
  empty、untracked 为 0。
- 真实会话只在目标 Python 3.8 子进程中验证 REAL_MODEL 与 GPU 双重授权、
  `materialsagent-zta35g` 环境和固定模型根。父进程四个相关变量在开始前均为
  UNSET，启动子进程后立即恢复，真实会话结束和默认回归前再次确认均为 UNSET。
- 主机总物理内存 `16539455488 bytes`（`15.404 GiB`）；开始空闲
  `9245163520 bytes`（约 `8.610 GiB`），加载前空闲
  `9037590528 bytes`（约 `8.417 GiB`），满足 8 GiB 硬门但低于 9 GiB
  推荐值。四次推理中的最低空闲内存为 A 的 `5150273536 bytes`
  （约 `4.797 GiB`），未触发 2 GiB 停止门；pytest 进程退出后的空闲内存为
  `9585262592 bytes`（`8.927 GiB`）。
- GPU 为 NVIDIA GeForce RTX 3060 Laptop GPU，driver `595.79`，
  Compute Capability `8.6`，总显存 `6144 MiB`；开始 used/free 为
  `0/5994 MiB`，计算进程 0，满足 5500 MiB 空闲硬门。Engine close 后、测试
  进程仍存活时，`summary.json` 观察到 CUDA context 尚存在，GPU used
  `1917 MiB`、compute process count `1`。pytest 子进程退出后的外部只读检查为
  used/free `0/5994 MiB`、compute process count `0`。两组数据分别代表对象关闭
  和进程退出，不把 Engine close 表述为同进程内销毁 CUDA context。
- 推理前和推理后 `SEM_INTEGRITY_OK` 均为 57 files、
  `2043071133 bytes`、aggregate fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；
  四份固定文件大小和 SHA-256 继续精确匹配门二值，`SEM/` 无 Git 变更。

### 12.2 固定输入、正式路径与调用预算

- 唯一输入为 solution `1000 °C / 3.0 h`、aging `730 °C / 3.0 h`；
  seed `20260730`，固定 `num_samples=1`、`guide_scale=2.0`、
  `timesteps=1000`。普通调用方仍不能覆盖后三项。
- 通过正式 `ZTA35GInferenceEngine` / `TorchZTA35GComponents` /
  `ModelBundleLoader` 路径加载一次固定
  `zta35g-sem-original-bundle`。CPU bundle load `3.867348 s`，GPU
  move/Engine load 阶段 `2.683464 s`，完整 Engine load `6.550812 s`。
- Engine device 为 `cuda`；DDPM 与 DenseNet 全部 parameters/buffers 位于
  `cuda:0`，SVR 保持 CPU。未使用 FP16、BF16、AMP、量化、CPU fallback、
  optimizer 或 backward。
- 固定序列为 A `["sem_image"]`、B `["mechanical_properties"]`、
  C `["sem_image","mechanical_properties"]`、D 为 C 的同 seed 重复。
  `engine_execute=4`、SEM generation/DDPM sampling `4/4`、mechanical /
  DenseNet / Yield predict / Elongation predict 均为 `3`，fit/score/backward/
  optimizer 均为 `0`。预算精确 `4/4`，无重试，未尝试第 5 次。
- A 不执行 DenseNet/SVR；B 内部生成一张 SEM，但完成输出集合仅为
  mechanical_properties，内部图片角色为 `intermediate_sem` 且
  `requested_output=false`；C 和 D 各只生成一张 SEM 并复用到性能链。

### 12.3 真实输出与重复性

- 四次 SEM 均为 NumPy `<f4` / float32、`[512,512]`、二维、C contiguous、
  NaN 0、Inf 0、非全常数。固定图像统计均为 minimum `-1.0`、maximum
  `1.0`、mean `0.09459365904331207`、standard deviation
  `0.43903008103370667`。
- B、C、D 的原始性能值均为 Yield Strength
  `413.39765052163557 MPa`、Elongation `2.9387915447083017 %`；
  两者有限且大于 0，没有 clipping、替换或异常值修复。
- A vs C SEM：exact `true`、allclose `true`、max/mean absolute
  difference `0.0/0.0`。B vs C 两项性能绝对差均为 `0.0`。
- C vs D SEM：exact `true`、allclose `true`、max/mean absolute
  difference `0.0/0.0`；Yield 与 Elongation 均 exact `true`、绝对差
  `0.0`。这是本机本次同 seed 观察，不扩展为跨硬件确定性承诺。

### 12.4 耗时与资源

- 完整推理原始耗时：A `515.474634 s`、B `594.399754 s`、
  C `635.616855 s`、D `640.459573 s`。真实 GPU compatibility
  `2 passed in 2396.89s (39:56)`，stderr 为空。
- 真实 pytest 实际在 `2396.89 s` 内正常完成；外层 45 分钟 timeout 的具体执行
  配置未持久化进入审查包，因此不把 watchdog 作为本门自动化通过证据。
- SEM generation 四样本原始耗时为
  `[515.470200, 594.316503, 635.578748, 640.422133] s`；
  minimum `515.470200`、maximum `640.422133`、mean `596.446896`、
  median/P50 `614.947625`。
- Mechanical prediction 三样本原始耗时为
  `[0.0786074, 0.0327715, 0.0327349] s`；minimum `0.032735`、
  maximum `0.078607`、mean `0.048038`、median/P50 `0.032772`。
- warm complete inference 取 B/C/D 三样本：
  `[594.399754, 635.616855, 640.459573] s`；minimum `594.399754`、
  maximum `640.459573`、mean `623.492060`、median/P50 `635.616855`。
  三组 P95/P99 均为 `insufficient_samples`；没有为分位数增加推理。
- A/B/C/D 的 torch peak allocated 均为 `1141142016 bytes`，peak reserved
  均为 `1761607680 bytes`。nvidia-smi peak used 分别为
  `3085/3093/3085/3085 MiB`，minimum free 分别为
  `2910/2902/2910/2910 MiB`，均未触发 400 MiB 安全门。
- A/B/C/D 主机最低空闲内存分别为
  `5150273536/5388128256/5532094464/5604921344 bytes`。单机四次数据是
  门三容量和耗时证据，不称为生产 SLA。

### 12.5 Payload 与人工复核产物

- 只复用 C 的 SEM 做 payload：`.npy` `1048704 bytes`，Base64
  `1398272 characters`，解码字节 `1048704`，完整安全 JSON 响应实际估算
  `1399715 bytes`，距 4 MiB 上限余量 `2794589 bytes`。
- serialization/Base64 encode/decode 分别为
  `0.0004809/0.0026477/0.0067135 s`。`allow_pickle=False` 解码保持
  dtype、shape 与值完全一致，Base64 解码字节一致；NPY SHA-256 与生产 payload
  SHA-256 均为
  `994a1bde4610ef7eda9a8447552f8d8b55cafe48ee22ee0433feb1f9e12ec578`。
- 受控目录为
  `tmp/p1b2-gpu-inference/20260730T082132Z-c339fc11686f/`，已由
  `git check-ignore` 确认为忽略路径，只含四个批准文件。NPY SHA-256 同上；
  review-only PNG 为 mode L、512×512、无 alpha、`review_only_preview=true`，
  SHA-256
  `be9a282cb44180f586700d9d258143193eeaeae4312a9ef34ccc776b58d34b1a`；
  `summary.json` SHA-256
  `5d5acab84cd8f4b905fdf080b593b205f99f591d9e7d1f4dda93a4a121408a96`；
  `artifact-manifest.json` SHA-256
  `8db89336c22d11be32bf9c00f1838a44509c15234c7b5888dfa48da914175419`。
- PNG 使用固定 review-only 映射，不作为 Backend 正式 PNG 编码一致性证据；
  人工目视仅观察到连续斜向层片/条带组织及清晰亮暗相边界，合理性结论仍由项目
  负责人判断。

### 12.6 测试、范围与状态

- TDD RED 为两个消费测试因共享 session fixture 尚未实现而精确 setup error；
  该阶段无授权、无权重加载、无 CUDA Tensor。仅 REAL_MODEL 授权时两项精确 skip，
  缺失 GPU 授权发生在权重加载和模型构造前；既有矩阵继续覆盖错误环境、
  非 Python 3.8 和 CUDA unavailable fail closed。
- 双授权 GREEN 为 `2 passed in 2396.89s`。授权恢复后，目标 Python 3.8 与
  Backend Python 3.11 的默认 Runtime 回归均为精确
  `143 passed, 3 skipped`；目标环境 `pip check` 为
  `No broken requirements found.`，两套 `compileall -q` 均退出 0。
- 生产 Runtime、Backend、Frontend、Mock Runtime、环境/锁、五份设计基线、
  migration 和 `SEM/` 均未修改；未启动 Runtime HTTP、Backend、PostgreSQL、
  MinIO 或 Frontend，未调用 DeepSeek 或浏览器 E2E。
- 门三已由项目负责人验收为 `COMPLETE / PROJECT_OWNER_ACCEPTED`；门四保持
  `NOT STARTED / NOT AUTHORIZED`。唯一验收提交已获授权；不执行 push 或 amend。

### 12.7 门三代码审查修订

- 初始审查结论 `P1B2_GPU_INFERENCE_CODE_REVIEW: CHANGES_REQUESTED` 只要求修订
  compatibility 测试工具的失败清理和证据措辞；修订完成后项目负责人最终审查结论为
  `P1B2_GPU_INFERENCE_CODE_REVIEW: APPROVED`。本轮三项真实授权变量保持未设置，
  真实权重打开、CUDA Tensor、DDPM 采样、模型 forward、SVR predict 和 GPU
  迁移均为 `0`；既有 A–D 真实 GPU 结果未重新执行。
- 加载资源 sampler 改为 fail-safe 生命周期：`start()` 成功后，
  `engine.load()` 成功、普通异常、包装后的 CUDA OOM 或 pytest 受控异常均由
  `finally` 精确尝试一次 `stop()`。首次停止会终止并等待 nvidia-smi 进程、join
  host/GPU 线程并核对均已退出；仍存活时固定拒绝
  `P1B2_RESOURCE_SAMPLER_SHUTDOWN_FAILED`。重复停止不会再次 terminate、kill
  或 join，成功时只返回首组摘要副本，失败时只重放固定错误。
- 加载 OOM 判定只接受当前 Torch 明确存在的
  `torch.cuda.OutOfMemoryError`；不再以全部 `RuntimeError` 作为缺省 OOM。
  生产 Engine 包装加载异常后，由测试 harness 检查既有 `oom_detected` 标志；
  OOM 路径先停止 sampler，再执行 Engine close、`gc.collect()` 和
  `torch.cuda.empty_cache()`，最后以无原始异常正文的
  `CUDA_OUT_OF_MEMORY` 结束。普通加载失败保持独立固定分类
  `P1B2_ENGINE_LOAD_FAILED`。
- TDD RED 为 6 个离线用例因新 helper fixture 尚未实现而精确 setup error；
  全部使用 fake sampler、fake Engine 或纯 helper，未进入真实 session fixture。
  GREEN 补充预算第五次前置拒绝、加载成功、普通失败、包装 OOM、pytest 受控异常、
  shutdown 残留和幂等停止，共 7 个用例；目标 Python 3.8 与 Backend Python
  聚焦测试均为 `7 passed, 1 deselected`。
- 清除授权变量后的两套默认 Runtime 回归均为精确
  `150 passed, 3 skipped`；三项 skip 分别为真实最小推理、真实模型加载和真实
  payload/resource compatibility。目标环境 `pip check` 为
  `No broken requirements found.`，两套 `compileall -q` 均退出 0。该修订不构成
  新的真实 GPU 推理证据，门三状态为
  `COMPLETE / PROJECT_OWNER_ACCEPTED`，门四仍为
  `NOT STARTED / NOT AUTHORIZED`。
- 最终 `SEM_INTEGRITY_OK` 仍为 57 files、`2043071133 bytes`、fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；
  `SCOPE_OK P1B2`。Git 仍为精确 7 个门三路径、staging empty、untracked 0，
  生产代码无 diff。受保护的 `check-scope.ps1` 与
  `test_payload_and_resources.py` SHA-256 仍分别为
  `9a14baa6556733e1c3991b75ddfa0369c54bb169a38404f8780f148f1c520f78` 和
  `b1febd2f5fff0a4ff8f5fccf91a1677573eaf9de547c438f7b8da558efb87210`，
  与本轮开始值一致。

门三最终状态：
`P1B2_GPU_INFERENCE_GATE_PROJECT_OWNER_ACCEPTED`。

## 十三、P1B2 门四真实 Runtime 与综合验收

### 13.1 恢复基线

- Branch：`main`。
- HEAD：`5735384430a7556682993de8861d2a7d28889156`。
- Subject：`test: validate zta35g minimal gpu inference`。
- Parent：`62e0273ff32bd1a7462abf2e9f33d898b993eb43`。
- 开始 working tree clean、staging empty、untracked 0；`git diff --check` 与
  `git diff --cached --check` 均无输出。

### 13.2 Backend Runtime timeout 配置阻塞

- 门四目标 Backend → Runtime execute timeout 为 900 秒。
- 实际生产环境变量名为 `ZTA35G_RUNTIME_TIMEOUT_SECONDS`。
- `AppSettings.zta35g_runtime_timeout_seconds` 当前验证范围为 `> 0` 且
  `<= 300` 秒。
- `parse_zta35g_runtime_config()` 将该字段写入
  `ZTA35GRuntimeConfig.timeout_seconds`；现有
  `LocalZTA35GToolClientAdapter` 随后将其实际传入
  `urllib3.Timeout(total=...)`。
- 因生产配置最大值低于 900 秒，固定阻塞码为
  `P1B2_BACKEND_RUNTIME_TIMEOUT_CONFIGURATION_BLOCKED`。
- DeepSeek timeout 仍为独立的 `DEEPSEEK_TIMEOUT_SECONDS`，默认 60 秒；
  本轮没有把 Provider timeout 改成 Runtime timeout。

### 13.3 停止边界

- 未修改 `backend/src/**`、`zta35g-runtime/src/**`、`frontend/src/**`、
  `SEM/**`、migration、依赖锁、Docker Compose 或五份设计基线。
- 未创建 `run-phase-1b.ps1`、`run-phase-1b.py`、真实 E2E 测试或运行手册。
- 未启动 Docker、PostgreSQL、MinIO、真实 Runtime、Backend 或 Frontend。
- 未读取或发现 `DEEPSEEK_API_KEY`、Runtime Token 或其他 Secret。
- Provider delegate：`0 / 5`。
- Runtime delegate：`0 / 2`。
- DDPM sampling：`0 / 2`。
- 未进入阶段 A SelfTest、阶段 B 真实 Runtime、阶段 C 浏览器 checkpoint 或
  阶段 D 回归。

### 13.4 当前状态

- 纯离线显式配置验证输出
  `P1B2_BACKEND_RUNTIME_TIMEOUT_CONFIGURATION_BLOCKED`。
- `SCOPE_OK P1B2`。
- `SEM_INTEGRITY_OK`：57 files、`2043071133 bytes`、aggregate fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- `git diff --check` 与 `git diff --cached --check` 通过；staging empty。
- 实际 diff 精确为本报告、当前进度和阶段计划 3 个文档路径。

P1B2 门四：
`BLOCKED / AWAITING_PROJECT_OWNER_REVIEW`。

本次门四尝试失败最终状态（历史）：
`P1B2_GATE4_BLOCKED`。

### 13.5 P1B2 门四前置生产配置修订

- 阻塞原因：门三实测单次真实推理最大耗时约 640.46 秒，而生产
  `ZTA35G_RUNTIME_TIMEOUT_SECONDS` 上限为 300 秒，无法合法采用门四要求的
  900 秒 Backend → Runtime execute timeout。
- 经项目负责人单独授权，只把
  `AppSettings.zta35g_runtime_timeout_seconds` 的最大合法值从 300 秒精确提高到
  900 秒；默认值保持 10.0 秒，最小值规则保持 `> 0`，字段名和正常环境解析入口
  均未改变。900 合法，901、0、负数和非数字值均由生产配置校验拒绝。
- `LocalZTA35GToolClientAdapter` 生产代码未修改。既有 fake PoolManager 合同测试
  证明配置值 900 被原样传给 `urllib3.Timeout(total=900)`，只发出一次 fake
  request，`retries=False`，没有真实网络调用。
- DeepSeek 生产代码未修改；独立字段 `DEEPSEEK_TIMEOUT_SECONDS` 默认仍为
  60 秒，`max_retries=0`。本轮 DeepSeek Provider calls 为 0，未读取或设置
  API Key。
- TDD RED 为 `1 failed, 96 passed`；唯一失败测试
  `test_runtime_timeout_accepts_gate4_upper_boundary` 精确因原生产约束
  `<= 300` 拒绝 900。单行生产修改后的聚焦 GREEN 为 `97 passed`，完整
  contract 为 `155 passed`。
- 权威受控离线回归 run id 为
  `20260730t111457z-71352452`。为遵守根目录 `.env` 不读取边界，没有直接调用会
  读取该文件且 Scope 参数不支持 P1B2 的 Phase 1A Runner；改用基于
  `git archive HEAD` 加本轮三个代码/测试 diff 的系统临时快照。该 Run 强制
  Mock LLM，启动唯一 PostgreSQL/MinIO 容器和受控 Mock Runtime，
  结果为 Backend unit `475 passed`、contract `155 passed`、Backend full
  `992 passed`、Mock Runtime `11 passed`、Backend `pip check` clean、
  `compileall` 通过、Alembic 唯一 head/current
  `0009_timeline_query_indexes` 且 `alembic check` clean。两个前置 harness
  尝试分别因测试进程环境污染和 `APP_ENV=test` 不满足既有 E2E `local` 前置而
  停止，均不属于生产代码失败；修正受控夹具后只执行上述一次权威回归。
  三次受控尝试的临时快照、容器和 Mock Runtime 均已清理。
- 固定 PostgreSQL 与 MinIO 镜像均声明 `VOLUME`，因此三个 Run 实际隐式创建了
  精确 6 个匿名卷，而非 0 个。清理审计按三个唯一 run 的秒级创建时间、Docker
  anonymous 标签和引用容器数 0 逐个证明所有权后，仅删除这 6 个精确匿名卷；
  未使用 volume prune，既有 `materialsagent_*` 与 `rag_system_*` 命名卷未修改。
- Frontend test 为 9 个 test files、`204 passed`；typecheck 与 build 均通过。
  Frontend 生产代码未修改。
- 本轮真实 Runtime 未启动、真实模型未加载、权重打开 0、CUDA Tensor 0、
  DDPM 0、SVR predict 0、真实 Provider 0。没有继续门四阶段 A/B/C/D，也没有
  消耗门四 Runtime 或 Provider 调用预算。

### 13.6 P1B2 门四 timeout 前置修订项目负责人验收

- 项目负责人代码审查结论：
  `P1B2_GATE4_TIMEOUT_PREREQUISITE_CODE_REVIEW: APPROVED`。
- 项目负责人确认生产修改仅为 ZTA35G Runtime timeout 最大合法值从 300 秒提高
  到 900 秒；默认值 10.0 秒和 `> 0` 最小值规则均未改变。
- `LocalZTA35GToolClientAdapter` 未修改；DeepSeek timeout 仍为独立的 60 秒，
  DeepSeek 生产代码未修改。
- 本次验收未启动真实 Runtime，未加载真实模型，真实 Provider calls 为 0。
- 审查包内未找到权威全量回归 run 的持久化 artifact；因此 Backend full
  `992 passed`、Mock Runtime、Frontend 与 Alembic 全量结果沿用实施报告记录，
  未在代码审查阶段重新独立执行。配置、Adapter、Unit、Contract、Scope 和 SEM
  已由审查包独立核验。

P1B2 门四 timeout 前置修订：
`COMPLETE / PROJECT_OWNER_ACCEPTED`。

P1B2 门四综合验收：
`NOT STARTED / AWAITING_PROJECT_OWNER_RESTART_AUTHORIZATION`。

### 13.7 P1B2 门四综合验收重新授权尝试

- 2026-07-30 项目负责人从正式基线
  `main@0db1c29f661313b10dac81c807f2649210046c1f` 重新授权门四综合验收。
  开始 working tree clean、staging empty、untracked 0；提交主题和父提交分别为
  `fix: allow zta35g runtime timeout up to 900s` 与
  `5735384430a7556682993de8861d2a7d28889156`，两项 Git diff check 均无输出。
- 按重新授权方案完整读取指定文档、既有验收脚本、Runtime、Backend、E2E 夹具和
  Frontend 文件后，确认 Runtime 入口、loopback 地址、Token、三条内部路由、
  Backend Runtime 配置链、900 秒 `urllib3.Timeout(total=...)`、
  `retries=False`、DeepSeek 60 秒 timeout、两条重试路由和 Vite 无主动代理
  timeout 均与既有实现精确映射。
- 在进入 Runner/Executor 实现或任何真实资源阶段前，发现重新授权方案的场景 5
  与场景 7 和当前生产重试语义冲突：场景 5 要求 Tool retry 后 Task 为
  `SUCCEEDED` 且自动 Explanation 成功；当前 Frontend 对成功 Explanation
  明确不显示 `retry-explanation`，Backend 也只允许已有失败 Explanation 且
  Task 为 `PARTIALLY_SUCCEEDED` 或 `FAILED` 时创建 Explanation retry。
- 最小复现使用现有 Frontend 测试：
  `npm test -- --run tests/components/tool-task-card.test.ts -t
  "offers Explanation retry only when Result exists and explanation needs it"`；
  结果为 1 个 test file、`1 passed | 16 skipped`。该用例明确断言成功
  Explanation 的卡片不存在 `retry-explanation`。Backend 既有
  `test_m8_explanation_retry.py` 同时锁定成功 retry 后使用新 key 再请求返回
  `409 / EXPLANATION_NOT_RETRYABLE`。
- 该冲突不能在本轮允许的 Runner、Executor、真实 E2E 或报告中绕过；修订
  `frontend/src/**`、Backend Explanation retry 生产语义或产品验收场景均需要
  项目负责人另行评审。按“生产缺陷立即停止”边界，本次未创建
  `run-phase-1b.ps1`、`run-phase-1b.py`、真实 E2E 测试或运行手册。
- 本次未读取或发现 Provider Key、Runtime Token 或其他 Secret；未读取
  权重，未创建 CUDA Tensor，未启动 Docker、PostgreSQL、MinIO、Runtime、
  Backend 或 Frontend。Provider delegate、Runtime delegate 和 DDPM sampling
  均为 `0`。

P1B2 门四综合验收：
`BLOCKED / AWAITING_PROJECT_OWNER_REVIEW`。

本次重新授权尝试最终状态：
`P1B2_GATE4_BLOCKED`。

### 13.8 P1B2 门四最终分阶段资源门修订

- 2026-07-31 项目负责人确认当前实施工作区的合法 Git 状态为：4 个 tracked
  modified、4 个授权 untracked、unexpected path 为 0、staging empty。当前实施
  阶段不再要求 `Untracked=0`；该要求只适用于未来项目负责人批准并完成验收提交
  之后。上述 8 个文件不得删除、暂存或提交。
- 统一 8 GiB、单点 6.75 GiB、五次采样中位数 6.70 GiB，以及 Stage B
  6.5 GiB / Stage C 6.25 GiB 方案均保留为历史记录但已废止，不得作为现行并行
  判断。此前 6.75 GiB 拒绝发生在 Runtime、权重和预算之前，因此不计入真实预算。
- Stage B 在设置真实模型授权、启动 Runtime 或打开权重前，使用
  `GlobalMemoryStatusEx.ullAvailPhys` 实际字节执行
  `5905580032 bytes`（5.5 GiB）硬门；少 1 byte 固定失败为
  `P1B2_GATE4_STAGE_B_MEMORY_PREFLIGHT_FAILED`，真实预算保持 0。
- Stage B 分别记录
  `stage_b_before_runtime_start_free_bytes`、
  `stage_b_after_runtime_ready_free_bytes`、`stage_b_pre_execute_free_bytes`、
  `stage_b_minimum_during_execute_free_bytes` 和
  `stage_b_post_execute_free_bytes`。Runtime ready 且尚未 execute 时至少等待
  2 秒；after-ready 与 pre-execute 各取 3 次、间隔 250 ms 的中位数。分别计算
  `stage_b_model_load_drop_bytes=max(0,before-start-after-ready)`、
  `stage_b_execute_drop_bytes=max(0,pre-execute-minimum-during-execute)` 和
  `stage_b_total_drop_bytes=max(0,before-start-minimum-during-execute)`。
- 场景 1–3 后，浏览器、Frontend、Backend、PostgreSQL 和 MinIO 已实际运行而
  Runtime 尚未启动时，Stage C 要求
  `stage_c_before_runtime_start_free_bytes >= max(4831838208,
  stage_b_model_load_drop_bytes+1610612736)`；失败固定为
  `P1B2_GATE4_STAGE_C_RUNTIME_START_MEMORY_FAILED`，不得打开权重或增加预算。
- Runtime ready 且模型已加载后至少等待 2 秒；Tool retry 前要求
  `stage_c_pre_tool_retry_free_bytes >= max(3758096384,
  stage_b_execute_drop_bytes+1610612736)`。只有通过才输出
  `P1B2_GATE4_TOOL_RETRY_RESOURCE_READY`；失败固定为
  `P1B2_GATE4_STAGE_C_TOOL_RETRY_MEMORY_FAILED`，不得发送 Tool retry、执行
  第二次 DDPM 或增加 Explanation 调用。
- Stage B 门覆盖模型加载、常驻模型和第一次推理；Stage C Runtime 启动门覆盖
  综合栈实时状态下的模型加载增量；Stage C Tool retry 门覆盖模型已加载后的
  单次推理增量。浏览器及综合栈占用已经包含在 Stage C 实时空闲内存读数中，
  不得把 Stage B 总下降量重复加入 Tool retry 门。
- 真实推理期间持续采样：低于 `2147483648 bytes`（2.0 GiB）记录
  `HOST_MEMORY_LOW_WARNING` 的首次时间、最低值和持续时间；低于
  `1610612736 bytes`（1.5 GiB）记录 `HOST_MEMORY_CRITICAL_LOW`。调用前命中
  critical 不得 delegate；调用中命中则等待返回后立即停止 Runtime、禁止后续
  场景并清理，且不得 CPU fallback、降低尺寸/timesteps 或启用 AMP/半精度。
- 该规则是针对当前约 15.4 GiB 物理内存主机的验收配置，目标是在浏览器和综合栈
  正常运行时尽量完成真实模型验收；它不是生产部署推荐配置，也不保证其他硬件
  具有相同性能。GPU 门、固定推理参数和调用预算保持不变。
- 最终资源规则的 fresh 离线证据：PowerShell/Python SelfTest 均输出固定成功
  标记，helper/E2E `65 passed, 1 skipped`，Python compile、`SCOPE_OK P1B2`、
  `SEM_INTEGRITY_OK`、tracked/cached/untracked whitespace 检查均通过。
- 最终规则下第一次 Stage B 尝试在 GPU 只读门以
  `P1B2_GATE4_RESOURCE_GATE_FAILED` 停止。根因为清洗后的 Windows 子进程环境
  未保留 NVML DLL 发现所需的普通系统路径 `ProgramFiles`/`ProgramW6432`；
  单变量诊断确认恢复该路径后 `nvidia-smi` 从 exit 255 变为 exit 0。Runner 已
  在不传递 Key 的前提下保留这两个系统变量，并增加离线回归。该失败未创建 run
  root、未启动 Runtime 或打开权重，Runtime/DDPM/Provider 预算仍为 0。
- 第二次 Stage B 尝试（run id `g4b745bf4426744c6653b57b1d`）启动 Runtime
  后在模型加载阶段固定失败，safe state 显示 Runtime/DDPM/Provider 预算均为 0。
  不打开权重的依赖探针确认清洗环境中 numpy/joblib/sklearn 正常、torch import
  抛出 RuntimeError；单变量恢复 `USERPROFILE` 后 torch import、CUDA available
  和设备计数均正常。Python 二次角色清洗已补齐该普通系统路径，Key 仍不传播。
  本次收尾最初报告 `P1B2_GATE4_CLEANUP_INCOMPLETE`；随后确认 PID 25188 已退出、
  8100 无监听、GPU used 0 MiB、无计算进程、Secret 扫描通过和 `.env` 哈希未变，
  再精确删除仅属于该 run 的临时目录。删除不可恢复，未触及正式资源。
- harmless owned-child 复现进一步确认 cleanup incomplete 的直接原因：Windows
  已终止进程在父侧 `Popen` 句柄仍持有时仍可查询 creation time，旧
  `owned_tree_gone` 因而把已终止进程误判为活动。Runner 改用
  `GetExitCodeProcess == STILL_ACTIVE` 判断活动状态；查询失败仍 fail closed，
  活动 PID 或 PID 重用仍不会被当作已清理。 retained-handle 回归和完整
  start/identity/taskkill/active-after 诊断均通过。
- `OpenProcess` 失败分支同时读取 `GetLastError`：只有
  `ERROR_INVALID_PARAMETER(87)` 作为 PID 不存在；access denied 与任何其他查询
  错误均保持 active/indeterminate 并使 cleanup fail closed。注入式不存在/拒绝
  访问回归均通过，PID 重用和身份校验边界不放宽。
- 第三次 Stage B 尝试 run id 为 `g42ecb990e686728656bdf99e9`。Runtime 仍在
  model loading 阶段安全失败；safe state 与事件日志确认 Runtime/DDPM/Provider
  均为 0，Secret 扫描通过，清理后端口、GPU 和 `.env` 均恢复。根因是 Runner
  错把 `ZTA35G_MODEL_ROOT` 指向 `SEM/`，而已确认 manifest 的四个 bundle 文件
  根目录为 `SEM/ZTA35G_lab/`。离线 RED/GREEN 已改用精确 bundle root，随后只
  删除该 run 临时目录，未触及正式资源。按三次失败审计边界，本轮不进行第四次
  真实尝试。

P1B2 门四综合验收：
`BLOCKED / AWAITING_PROJECT_OWNER_REVIEW`。

本次资源门修订与真实执行状态：
`P1B2_GATE4_BLOCKED`。

### 13.9 P1B2 门四第四次且最终一次 Stage B 真实 Runtime 直连

- 2026-07-31 项目负责人明确授权第四次且最终一次 Stage B；授权不包含
  Stage C、Backend、PostgreSQL、MinIO、Frontend、浏览器或 DeepSeek。执行前
  `main@0db1c29f661313b10dac81c807f2649210046c1f`、4 个 tracked modified、
  4 个授权 untracked、unexpected 0、staging empty；前三次均在 delegate 前
  失败，因此起始 Runtime/DDPM/Provider 预算为 `0/2`、`0/2`、`0/5`。
- 唯一模型根由仓库根规范化解析为相对路径 `SEM/ZTA35G_lab`，四份文件的大小和
  SHA-256 精确匹配门二已验收值。启动前
  `GlobalMemoryStatusEx.ullAvailPhys=6510964736 bytes`，GPU 为 NVIDIA
  GeForce RTX 3060 Laptop GPU、总显存 6144 MiB、空闲 5994 MiB、Compute
  Capability 8.6、计算进程 0；`SEM_INTEGRITY_OK`。
- run id `g452f78ce7e468dea983977272` 的唯一 Runtime PID 为 `5432`，正式
  Python 3.8 环境只监听 `127.0.0.1:8100`。ready 响应为 `status=READY`、
  `model_loaded=true`、model files/device status 均为 `AVAILABLE`、
  `device.kind=cuda`、bundle 为 `zta35g-sem-original-bundle`；模型加载约
  `8.673 s`。Runtime 子进程未获得 `DEEPSEEK_API_KEY`，Provider 调用为 0。
- 缺 Token、错误 Token、正确 Token 以及错误 tool name/version、固定参数偏离、
  非法 requested outputs 和越界输入均按正式 HTTP 合同完成负测；真实 delegate
  增量为 0。唯一 combined execute 取得执行槽后，并发合法请求在约 `0.015 s`
  返回 `503 / RUNTIME_BUSY / retryable=true`，未增加 delegate。
- 唯一真实 execute 在约 `632.797 s` 后返回 `SUCCEEDED`，requested/completed
  outputs 精确为 SEM image 与 mechanical properties，failed outputs 为空，
  三个 ID 和 bundle 均按请求/合同回显。Runtime/DDPM/Provider 最终预算分别为
  `1/2`、`1/2`、`0/5`，没有第二次正常 execute、自动重试、CPU fallback、
  AMP/半精度、尺寸或 timesteps 调整。
- 进程内响应检查通过 `base64+npy`、`allow_pickle=False`、`dtype=<f4`、
  `[512,512]`、二维、C contiguous、finite、值域边界 `[-1,1]` 以及 NPY
  SHA-256 与响应声明一致；Yield Strength 为
  `413.39765052163557 MPa`，Elongation 为
  `2.9387915447083017 %`。但是当前 Runner 未把精确 NPY bytes、Base64
  characters、JSON bytes、图片 SHA-256、实际 min/max 持久化到无 Secret
  summary，也未显式断言或保存“非全常数”证据。Runtime 停止后临时响应已按
  强制清理要求删除，且禁止第二次 execute，因此这些必填证据不能补采或猜测。
- 主机内存实测：before-runtime-start `6515011584 bytes`、after-ready
  `5061283840 bytes`、pre-execute `5080629248 bytes`、minimum-during-execute
  `2376253440 bytes`、post-execute `3393437696 bytes`；model-load drop
  `1453727744 bytes`、execute drop `2704375808 bytes`、total drop
  `4138758144 bytes`。最低值高于 2.0 GiB warning 门和 1.5 GiB critical 门，
  因此 warning/critical 均未触发。GPU peak used 为 3085 MiB、minimum free
  为 2910 MiB。
- Secret 精确扫描通过；`.env` 开始/结束 SHA-256 均为
  `11ef1a30f325ad5cc3c25538971301ec4b16bb75a4720bdc066c8035758a935c`，
  未修改且未进入 artifact。Runtime 精确 PID 已终止，8100 无监听，GPU used
  恢复 0 MiB、计算进程 0，资源轮询器与授权环境已恢复；本次 run root 和辅助
  临时目录均已精确删除。没有创建数据库、bucket 或正式资源。
- 清理后的 fresh 离线复核：PowerShell/Python SelfTest 均输出固定成功标记；
  helper/E2E `65 passed, 1 skipped`；Python compile、`SCOPE_OK P1B2`、
  `SEM_INTEGRITY_OK`、tracked/cached/untracked whitespace 检查均通过。

第四次真实 Runtime direct 执行本身：
`SUCCEEDED`。

上述精确元数据未持久化作为非阻塞证据限制保留。相同固定输入、seed、bundle 和
主机环境已经在门三独立验证这些字段；项目负责人决定不消耗第二次 Runtime 预算
重复 Stage B，不得补写未持久化的精确数值。

项目负责人当时审阅结论（Stage C Provider 事故后已失效，仅作历史记录）：

```text
P1B2 门四 Stage B：
COMPLETE / PROJECT_OWNER_ACCEPTED

第五次 Stage B：
NOT AUTHORIZED

P1B2 门四：
HISTORICALLY_AUTHORIZED / CURRENT_RUN_INVALIDATED
```

该历史授权原计划从 Runtime `1/2`、DDPM `1/2`、Provider `0/5` 继续；Provider
事故后其中 Provider `0/5` 已证明不具权威性，整个 Stage C run 永久作废。剩余
Runtime/DDPM 事实不授权继续执行，不得再次运行 Runtime direct。

### 13.10 P1B2 门四 Stage C 初始综合栈阻塞

- Stage C 离线接线先以 RED/GREEN 补齐 Stage B 已接受预算的恢复语义：
  新 Stage C run 从 Runtime `1/2`、DDPM `1/2`、Chat `0/3`、
  Explanation `0/2` 开始，预算账本跨 Backend/Runner 重建不归零，并继续在
  第三次 Runtime 或第六次 Provider delegate 前拒绝。
- 离线门通过：PowerShell/Python SelfTest 输出固定成功标记，helper/E2E 最终
  `71 passed, 1 skipped`，Python compile、`SCOPE_OK P1B2`、
  `SEM_INTEGRITY_OK` 和 Git whitespace/boundary 检查通过。`.env` 开始/结束
  SHA-256 保持
  `11ef1a30f325ad5cc3c25538971301ec4b16bb75a4720bdc066c8035758a935c`。
- Stage C run id 为 `g456aaa9615b3b4bd08bd1d9e9`。初始 Phase1 在 Compose
  门失败；单变量诊断确认 PowerShell Phase1 二次清洗遗漏普通系统变量
  `PROGRAMFILES`/`PROGRAMW6432`，导致 `docker compose` 不可发现。StageB 与
  Phase1 现共用同一普通系统变量白名单，PowerShell SelfTest 以 RED/GREEN 锁定。
- 越过 Compose 后，Frontend 原 PowerShell `-Command` 包装把
  `C:\Program Files\...` 下的 Node 路径截断。Runner 改为直接启动 Node，并用
  Node process title 保存安全 ownership marker；路径含空格和进程启动/停止均有
  离线/隔离诊断覆盖。
- Windows retained-handle 语义同时补入 Phase1 回滚：已退出进程不再仅因仍可
  读取 start time 被误判为活动；回滚在 Secret 扫描后只删除本轮 transient logs，
  保留安全 state 和预算。相关 RED/GREEN 均通过。
- 尽管 Compose、Backend、Vite 各单独边界均已验证，完整 Phase1 仍固定命中
  `P1B2_GATE4_PROCESS_IDENTITY_FAILED`。按系统化调试停止条件，不再重复启动或
  叠加假设；未输出 `P1B2_GATE4_BROWSER_ACCEPTANCE_READY`，浏览器场景 1–3
  未开始，Runtime 始终停止，Provider/Runtime/DDPM 均无新增 delegate。
- 回滚后 state 精确恢复到 `STAGEB_COMPLETE`，预算为 Runtime `1/2`、
  DDPM `1/2`、Chat `0/3`、Explanation `0/2`，process/resources 为空。
  3000/8000/8100/5432/9000/9001 无监听，GPU used 0 MiB、计算进程 0；
  临时 database 与 bucket 均不存在，命名 volume 数量与 fingerprint 前后相同，
  `.env` 未改变。
- 清理后的完整 Mock 回归首次手工执行没有先在测试子进程覆盖
  `LLM_ADAPTER=mock`；根 `.env` 的现行值为 `deepseek`，因此该测试进程实际
  构造了真实 Provider Adapter。单文件诊断明确观察到至少 8 个本应在 Mock 模式返回
  503 的消息请求返回 200；此前同环境的 Backend full 还运行了 300 秒后才由
  外层 watchdog 终止。临时测试数据库随后已全部清理，无法再从 LLMCall 表精确
  恢复 delegate 总数。因此 Runner 持久账本虽然仍为 Chat `0/3`、
  Explanation `0/2`，但实际 Provider 预算已经不可审计，且至少存在 8 次真实
  Chat delegate；不得继续宣称 Provider `0/5` 或预算合规。
- 发现后立即停止非 Mock 测试，后续所有 Backend 回归均以仅对子进程生效的
  `LLM_ADAPTER=mock` 重跑：Backend full `1063 passed, 1 skipped`、Mock
  Runtime `11 passed`、Frontend `204 passed`，Backend/Runtime `pip check`、
  两套 compile、Frontend typecheck/build 以及 Alembic head/current/check 均
  通过。该受控回归不能消除前述真实 Provider 预算事故。
- 最终资源审计再次确认 Stage C database/bucket 不存在、测试数据库数量为 0；
  Mock 栈按所有权停止，目标端口无监听，本轮 `tmp/p1b2-gate4` 在 Secret 扫描
  通过后已精确删除。

P1B2 门四 Stage B：
`COMPLETE / PROJECT_OWNER_ACCEPTED`。

Provider 隔离事故发生时的 P1B2 门四 Stage C 状态：
`BLOCKED / SAFETY_REMEDIATION_REQUIRED`。

事故记录标记：
`P1B2_GATE4_PROVIDER_BUDGET_INCIDENT_RECORDED`。

### 13.11 P1B2 门四 Stage C Provider 隔离事故

事故事实：

1. 清理后的手工 Mock 回归未显式强制 `LLM_ADAPTER=mock`。
2. 测试进程读取根 `.env` 中的 deepseek 模式并构造真实 Provider Adapter。
3. 已明确观察计划外真实 Chat delegate `>= 8`；这是已知下界，不是精确总数。
4. 同环境另有一次约 300 秒运行；临时测试数据库随后已清理，精确调用总数不可
   恢复。
5. Runner 账本中的 Provider `0/5` 不再具有权威性，不得删除、归零或并入未来
   新 run。
6. 当前 Stage C 验收运行整体作废，状态为
   `INVALIDATED / PROVIDER_BUDGET_UNAUDITABLE`。
7. Secret 精确扫描无命中；`.env` 开始和本次整改复核 SHA-256 均为
   `11ef1a30f325ad5cc3c25538971301ec4b16bb75a4720bdc066c8035758a935c`，
   文件未修改。
8. Stage B 的真实 Runtime direct `SUCCEEDED`、Runtime `1/2` 和 DDPM `1/2`
   已接受证据继续有效；不得执行第五次 Stage B。

事故状态：
`P1B2_GATE4_PROVIDER_BUDGET_INCIDENT_RECORDED`。

### 13.12 Stage C 安全整改

#### Mock Provider 隔离

- 门四期间禁止直接手工执行 Backend full 或任何可能继承 `.env` Provider 模式
  的 pytest 命令。唯一允许的 helper/E2E 回归入口是受控 Runner。
- 安全整改初次代码审查结论为
  `P1B2_GATE4_STAGE_C_SAFETY_REMEDIATION_CODE_REVIEW: CHANGES_REQUESTED`。
  重要问题 1 是 `MockRegression` 当时只执行
  `test_real_zta35g_journey.py`，没有保护 Backend full 和完整 Phase 1A Mock
  回归；重要问题 2 是进程身份只验证 marker，没有锁定精确 argv。
- 项目负责人最终代码审查结论为：

  ```text
  P1B2_GATE4_STAGE_C_SAFETY_REMEDIATION_CODE_REVIEW:
  APPROVED
  ```

  因此 P1B2 门四 Stage C 安全整改状态更新为
  `COMPLETE / PROJECT_OWNER_ACCEPTED`。
- 入口现已拆分为 `MockHelperTests` 与 `MockFullRegression`；兼容入口
  `MockRegression` 固定映射到完整回归，不再映射到 helper。完整回归复用既有
  `run-phase-1a.ps1`，在系统临时目录从固定 HEAD 创建只含已跟踪文件的干净
  快照；不修改真实 index/working tree，不复制或链接根 `.env`，回归后删除
  临时 `.env`、快照和本轮 owned Mock 资源。
- 独立只读复核进一步发现：启动命令若在部分创建 Mock 资源后失败，旧包装器会
  因“成功 marker 尚未出现”而不执行 stop。修复后在发送 start 命令前即记录
  cleanup responsibility；无论 start 返回成功、非零、超时或缺 marker，都进入
  幂等 stop，并核对 3000/8000/8100、5432/9000/9001 与固定
  `materialsagent` Compose 容器集合。基线若已有目标 listener/container 则在
  启动前 fail closed，不接管未知资源。
- 修改实现前的聚焦 RED 为 `10 failed, 103 passed, 1 skipped`：完整回归 CLI
  尚不存在、marker 仍存在时脚本/action 或参数增删重排仍会误通过、账本预留
  后异常退出仍可被视为 `EXACT`。这些 RED 均来自预期行为缺口，不是 fixture、
  import 或语法错误。
- Mock 子进程显式获得 `LLM_ADAPTER=mock` 与空白
  `DEEPSEEK_API_KEY`，并移除 M12-B 和 P1B2 的全部真实调用授权变量。根 `.env`
  中即使存在 deepseek 模式和 canary Key，也不能进入测试子进程。
- pytest collection 前在同一精确环境完成 preflight：
  `AppSettings().llm_adapter == "mock"`，DeepSeek Chat/Explanation Adapter
  构造次数 0，真实 Provider delegate wrapper 调用次数 0。任一失败均固定为
  `P1B2_GATE4_MOCK_PROVIDER_ISOLATION_FAILED`。

#### 权威 Provider delegate 账本

- 该文件是 **Provider delegate 边界的权威保守计数**，不是无条件等同于
  Provider 实际收到的网络请求数。正常、无进程崩溃的验收流程中，账本应与
  当前 run 的 DeepSeek `LLMCall` 数据库事实一致。
- Runtime/DDPM 账本不再保存 Provider 快照。Provider 文件账本字段精确为
  `schema_version`、`run_id`、`chat_delegate_attempts`、
  `explanation_delegate_attempts`、`total_delegate_attempts`、
  `maximum_delegate_attempts`、`state`、`updated_at`；不保存 Prompt、用户
  正文、模型回复、Key、Token、Base64 或异常正文。
- 每次真实 Provider Adapter delegate 前均获取独占锁，读取并验证账本 run id、
  状态、计数单调性和预算；attempt 先增加，经 fsync 临时文件和原子替换写入
  正式账本，重新读取确认并与当前 run 的 `LLMCall` purpose 计数核对后，才允许
  调用 Adapter。
- 真实 Key Backend、无效 Key Backend、恢复真实 Key Backend 均通过
  `create_app()` 的显式 Chat/Explanation port 注入使用同一账本。第六次调用在
  delegate 前拒绝。应用工厂若失去显式注入能力，固定为
  `P1B2_GATE4_PROVIDER_LEDGER_INJECTION_BLOCKED`；未修改生产 Backend。
- 账本不存在、损坏、run id 不匹配、字段缺失、计数回退、数据库事实冲突、
  原子替换失败或子进程退出后无法读取，均进入
  `P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN`；状态只能为
  `EXACT/UNKNOWN/INVALIDATED`，UNKNOWN 后禁止任何后续真实调用。
- 如果进程在账本 attempt 预留完成后、Adapter 调用前异常退出，账本可能保守
  多计，但不会少计，也不会允许超预算调用。该 run 必须保持 `UNKNOWN` 或
  `INVALIDATED`，新的 Backend 不得把它恢复成可继续的 `EXACT` run，且不得写
  `PASSED`。
- 当前污染 run 永久保持 INVALIDATED 与历史计划外调用 `>= 8`。未来只有项目
  负责人重新授权后才可创建不同 `run_id`、ledger、database、bucket、Actor 和
  Conversation，并把“历史作废 run”与“当前 fresh run”分开报告。

#### 进程所有权与 Phase1 原子回滚

- 所有权验证不再只比较 executable basename 和命令行子串。安全记录包含
  role/port、PID、start time、完整 executable path 的 SHA-256、
  `command_argument_count`、`command_arguments_sha256`、command marker 与
  相对日志路径。参数摘要严格按原顺序将 `argv[1:]` 序列化为 UTF-8 JSON 数组
  （`ensure_ascii=True`、`allow_nan=False`、紧凑 separators）后计算 SHA-256；
  不保存完整参数文本。
- Python/Node 路径含空格、参数含引号及活动 retained handle 正例通过；进程
  已退出、PID 重用、start time、executable、参数增删/重排、脚本或 action
  变化、marker 或 port owner 不匹配均固定拒绝
  `P1B2_GATE4_PROCESS_IDENTITY_FAILED`。marker 即使仍存在，只要完整参数摘要
  不同也以 `failure_field=command_arguments` 拒绝。role 由 record schema、
  精确 argv 和唯一 marker 共同约束；port 的独立证据是实际 LISTENING 地址与
  `port_owner_pid`。诊断只输出
  `role`、`pid`、`expected/actual` 布尔值和 `failure_field`。
- Phase1 的 Backend、Frontend、健康检查或 ownership 任一步失败，均停止本轮
  owned process、删除本轮精确 bucket/database、释放端口、恢复环境，并把 run
  标成 `INVALIDATED`；不再回退到可复用 `STAGEB_COMPLETE`。只有资源已经完全
  清理时，未来重试才可删除旧 run root 并生成不同 run id。

#### 当前执行边界

- 本次整改真实 Provider、Runtime execute、DDPM、权重打开、CUDA Tensor、
  真实 Backend、真实 Frontend 与浏览器验收调用均为 0。完整 Mock 回归按授权
  复用 Phase 1A 的受控 PostgreSQL、MinIO、Mock Runtime、Mock Backend 和
  Frontend 栈；这些 owned Mock 资源在回归结束后已完整清理。
- Python 和 PowerShell 的 Stage B 入口固定拒绝第五次尝试；Phase1 入口固定
  拒绝新的 Stage C。当前只允许 SelfTest、受控 Mock helper/E2E、受控完整
  Mock 回归与静态检查。
- 本轮新执行 Python SelfTest：
  `P1B2_GATE4_EXECUTOR_SELF_TEST_OK`；PowerShell SelfTest：
  `P1B2_GATE4_EXECUTOR_SELF_TEST_OK`、
  `P1B2_GATE4_POWERSHELL_SELF_TEST_OK`。
- 受控 Mock helper/E2E 回归为 `117 passed, 1 skipped`，并输出
  `P1B2_GATE4_MOCK_HELPER_TESTS_OK`；唯一 skip 是需要四项真实授权的真实旅程
  测试，本轮按安全边界不得授权。
- 受控完整 Mock 回归入口 `MockFullRegression` 输出
  `P1B2_GATE4_MOCK_FULL_REGRESSION_OK`。Phase 1A run id 为
  `20260731T064439Z-c941e3f9aa0b`：Backend E2E `24 passed`、Backend full
  `992 passed`、Mock Runtime `11 passed`、Frontend `204 passed`；
  Frontend typecheck/build、Alembic head/current/check、Backend pip check、
  compileall、SEM 与 M12A Scope 均通过。回归前后 DeepSeek `LLMCall` 增量 0、
  DeepSeek Adapter 构造 0、真实 Provider 账本无增量；临时快照 Secret 扫描和
  owned 资源清理均通过，根 `.env` SHA-256 前后一致。
- 干净快照及动态产物在成功清理后删除，最终审查包没有保留原始 Phase 1A
  动态文件；保留的是由受控 Runner 从实际日志解析、校验后输出的安全摘要。
- 修复部分启动清理与 `INVALIDATED` 状态保持后，独立只读复审结论为
  `NO_BLOCKING_FINDINGS`；本轮三项代码审查要求均已关闭。
- 两个 Python 文件 `py_compile` 通过；Scope 为 `SCOPE_OK P1B2`；SEM 为
  `SEM_INTEGRITY_OK`（57 files，2,043,071,133 bytes）；tracked、
  cached 和 4 个 authorized untracked whitespace check 均通过。

当前状态：

```text
P1B2 门四 Stage B:
COMPLETE / PROJECT_OWNER_ACCEPTED

P1B2 门四 Stage C 安全整改:
COMPLETE / PROJECT_OWNER_ACCEPTED

当前 Provider 预算:
UNAUDITABLE / CURRENT_RUN_INVALIDATED

新的真实 Stage C:
NOT STARTED / AWAITING_PROJECT_OWNER_AUTHORIZATION
```

下一步：创建安全整改唯一验收提交并恢复干净工作区。项目负责人重新授权后，
使用全新的 `run_id`、Provider 账本、database、bucket、Actor 和 Conversation
执行 Stage C 核心真实浏览器闭环；不得恢复或复用污染 run。

安全整改审查状态：
`P1B2_GATE4_STAGE_C_SAFETY_REMEDIATION_CODE_REVIEW_APPROVED`。

## 十四、P1B2 门四-A 前置 Chat Orchestration Harness 完善

### 14.1 基线、现象与根因

- 本工作单元从干净基线
  `main@b074a89563e94bf18419d5bff865636b71326885` 开始；staging、tracked
  diff 和 untracked 均为空。项目负责人只授权 Chat Orchestration Harness
  完善，不授权新的真实 Stage C、真实 DeepSeek、真实 Runtime、GPU 或权重操作。
- 真实浏览器体验中，普通用户同时要求生成 SEM 图像并预测屈服强度和延伸率，
  Chat Orchestration 却只产生 `requested_outputs=["sem_image"]`；把同一意图改写
  成明确内部字段后，图像和性能均成功。既有 Runtime、性能模型和确定性校验因此
  不是该遗漏的根因。
- 旧 Chat system prompt 主要说明 JSON shape；唯一完整 Tool 示例和唯一
  `NEEDS_INPUT` 示例都只使用 `sem_image`。当前 `json_mode` 不会把本地 Pydantic
  字段 description 发送给 Provider，因此模型消息中没有获得屈服强度/延伸率、
  并列请求、明确排除与中间 SEM 的完整交付语义。Tool Registry 已知的能力与限制
  也没有进入模型消息。
- Adapter 原样把 Provider 输出映射为 `ToolCandidate`；Application 只负责单位、
  范围、精度、Schema、完整性和输出允许值/去重，不允许重新猜测 LLM 已遗漏的
  输出。缺口因此定位在单次 Chat Harness 的模型输入边界。

### 14.2 最小 Harness 方案与离线评测

- Prompt template 从 `chat-orchestration/v2` 升为 `v4`。system context 明确唯一
  Tool 只适用于 ZTA35G，使用四维热处理参数，可交付 SEM 图像、由屈服强度与
  延伸率组成的力学性能，或两者同时交付。
- `requested_outputs` 被定义为“用户要求的交付项”，不是 Tool 内部计算步骤：
  只请求性能时仍会内部生成中间 SEM，但不得因此把 `sem_image` 加入用户交付；
  同时请求时必须保留两项；明确排除优先作用于被排除的交付项。
- 已核验当前 LangChain `with_structured_output(..., method="json_mode")` 路径：
  Pydantic Schema 用于本地 `PydanticOutputParser`，Provider 侧只获得 JSON mode
  response format，不会获得字段 description。因此 route、Tool、material、四维
  候选参数、requested outputs、missing/ambiguous 和 follow-up 的关键语义及 JSON
  示例均直接写入实际发送的 system message；未切换 Tool Calling 或其他 Provider
  模式。Pydantic 继续只负责本地严格解析和 route shape 校验。
- 离线合同矩阵覆盖：只图像、只性能、图像+性能、非内部字段的自然表达、明确排除
  性能、明确排除图像、缺少时效时间，以及材料缺失。所有 Tool 执行正例的用户文本
  都明确包含 `ZTA35G`；材料缺失例保持 `material=null`、进入 `NEEDS_INPUT`，并保留
  `requested_outputs=["sem_image","mechanical_properties"]`。
- fake Runnable 会按 fixture 直接回放预置 payload。它只证明 Provider 可见 messages
  的组成、本地 Pydantic 解析、`DeepSeekChatAdapter -> Domain` 映射和一次 invoke
  合同，不能证明自然语言样例被模型正确理解，也不能证明真实 Provider Prompt
  质量。该质量评测需要后续单独授权。生产代码没有关键词路由、规则补丁、judge、
  repair、retry、fallback 或第二次 LLM 调用。

### 14.3 RED、GREEN 与回归

- 初版修订曾记录 `3 failed, 7 passed -> 10 passed in 1.47s`；其中把 Pydantic
  description 当作模型可见 Harness 的断言经独立审查确认不成立，因此该组结果不再
  作为 Provider Prompt 合同证据，只保留为审查前历史。
- 独立审查修订 RED 为 `2 failed, 8 passed`：Provider 可见 message 缺少材料不得
  猜测/材料缺失 JSON 示例，且 metadata 仍为 v3。最小修订后同一隔离命令为
  `10 passed in 1.34s`。原先依赖 Pydantic description 的测试已删除，替换为直接
  检查 Provider 实际接收 messages 的合同测试。
- 独立审查修订后的新鲜相关回归为：Backend Unit+Contract 全量
  `639 passed in 8.23s`；消息编排、消息幂等、Chat 持久化、Provider 幂等与
  DeepSeek wiring 共 `52 passed in 31.11s`；Mock Runtime 全量
  `11 passed in 1.07s`。Backend `pip check` 无破损依赖，Backend/测试/Mock
  Runtime `compileall -q` 通过。
- Chat/DeepSeek/ZTA35G 输入相关回归为 `205 passed in 1.93s`；Backend Contract
  全量为 `164 passed in 3.80s`；Backend Unit 全量为
  `475 passed in 6.47s`。最终完整回归只采用下述隔离分组证据，不重复累计这些
  中间结果。
- 首次 Backend full 以基础设施变量污染整个 pytest 进程，得到
  `1107 passed, 1 skipped, 11 failed`；11 项全部是配置负例被父环境补齐，不是
  Harness 断言失败。随后把 Unit/Contract 与 integration/llm 错分为无基础设施
  组，得到 `644 passed, 2 setup errors`；两项错误来自 provider idempotency
  用例实际依赖 API PostgreSQL fixture。两次失败命令均不计入最终通过证据。
- 最终启动器在 pytest 导入 fixture 前，把进程内 `ROOT_ENV_FILE` 指向确认不存在
  的普通临时路径；完整集合按互斥环境分组执行：Unit+Contract
  `639 passed in 8.46s`，API+Integration+E2E
  `478 passed, 1 skipped, 1 deselected in 173.19s`，被分离的唯一配置负例
  `1 passed in 0.03s`。合计 `1118 passed, 1 skipped`，覆盖本轮收集的全部
  1119 个 Backend 用例；唯一 skip 是未授权真实 P1B2 旅程。
- Mock Runtime 全量 `11 passed in 0.94s`；Backend `pip check` 为
  `No broken requirements found.`；Backend/Backend tests/Mock Runtime
  `compileall -q` 通过。临时数据库从空库 upgrade 到
  `0009_timeline_query_indexes (head)`，current 为 head，Alembic check 输出
  `No new upgrade operations detected.`。

### 14.4 真实调用、安全边界与资源恢复

- 本工作单元真实 DeepSeek/Provider 调用 0，真实 Runtime 启动 0、真实权重打开
  0、GPU/CUDA 使用 0；没有启动 Frontend 或浏览器。测试中的 DeepSeek Adapter
  均注入 fake Runnable/model，Provider socket 调用没有发生。
- 完整 Mock 回归使用两个本轮唯一命名、数据目录为 tmpfs 的临时 PostgreSQL/
  MinIO 容器，以及一个明确显示 `device.kind=cpu` 的 Mock Runtime 进程。没有
  使用 Compose、命名卷、真实 Runtime 或 GPU。
- 起始 Docker daemon 未运行；回归后精确停止 Mock Runtime PID，检查并删除两个
  本轮容器，再通过 Docker 自带 shutdown 恢复 daemon 未运行。既有六个命名卷
  及停止容器未修改，3000/8000/8100/5432/9000/9001 均恢复无监听。本轮临时
  stdout/stderr 文件逐文件删除后移除空临时目录。
- 本轮没有直接打开、显示或修改根 `.env`。但最初两次普通 pytest 编排中，代码
  走到无参 `load_settings()` 时 Pydantic 可能按生产默认路径只读解析过根 `.env`；
  没有输出其内容，也没有修改文件。这是回归编排边界错误。最终完整证据全部通过
  导入前覆盖 `ROOT_ENV_FILE` 的隔离启动器获得，不再访问根 `.env`。
- 独立审查修订的所有 pytest 命令从首次 RED 开始都在导入 fixture 前把
  `ROOT_ENV_FILE` 指向确认不存在的路径，未读取或修改根 `.env`。
- 独立审查修订的 52 项数据库相关回归只启动一个唯一命名、PostgreSQL 数据目录为
  tmpfs 的临时容器；未启动 MinIO、Frontend、Mock/真实 Runtime、Provider 或 GPU。
  容器经名称与 `HostConfig.Tmpfs` 所有权校验后精确停止删除，Docker 恢复到起始
  未运行状态；既有停止容器和六个命名卷未修改。

### 14.5 独立复审、项目负责人验收与停止点

- 精确允许范围为 DeepSeek Chat Adapter、既有 DeepSeek 合同、独立 Harness
  评测、P1B2 Scope 以及阶段计划/进度/Phase 1B 报告共 7 个路径。
- Frontend、真实/Mock Runtime 实现、migration、模型、权重、五份设计基线和
  Application/Domain 均无 diff。独立复审结论为 `APPROVED`，Critical、Important、
  Minor 均为 0；项目负责人据此验收本工作单元。
- `SCOPE_OK P1B2`；`SEM_INTEGRITY_OK` 为 57 files、`2043071133` bytes、
  aggregate fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- 状态为 `COMPLETE / PROJECT_OWNER_ACCEPTED`。验收只授权将精确 7 个路径纳入一个
  由 Git 生成 hash 的唯一验收提交，不授权 push、amend、第二提交或新的真实
  Stage C。本报告中的离线 fixture replay 仍只证明 Harness 消息结构、本地解析、
  Adapter/domain 映射与单次 invoke 合同；真实 DeepSeek Prompt 质量尚未验证，
  需要未来单独授权。
- 验收收尾新鲜复验：聚焦合同 `10 passed in 1.48s`；Backend Unit+Contract
  `639 passed in 9.44s`；消息编排/幂等、Chat 持久化、Provider 幂等和 DeepSeek
  wiring `52 passed in 24.64s`；Mock Runtime `11 passed in 1.12s`；`pip check`
  无破损依赖，`compileall -q`、`SCOPE_OK P1B2` 和 `SEM_INTEGRITY_OK` 通过。
  所有 pytest 从 fixture 导入前隔离根 `.env`，真实 DeepSeek、Runtime 和 GPU
  使用均为 0。本轮唯一 tmpfs PostgreSQL 容器已精确删除；Docker 在数据库门之前
  已为外部预存运行状态，因此验收收尾保留其运行，不改变既有容器和命名卷。

## 十五、P1B2 门四-A 路线调整与当前结论

### 15.1 已完成的本地人工闭环

- 项目负责人确认，首次真实 DeepSeek 与真实 ZTA35G Runtime 同时启用的浏览器
  闭环已经完成。
- 该结果表明当前平台能够用于本地开发、功能探索和人工体验。它是项目负责人对
  本地使用价值的确认，不是正式 Stage C Runner 的生产级进程、Secret、预算、
  审计和原子清理验收。

### 15.2 正式 Runner 暂缓

- 正式 Stage C 五阶段 Runner 的安全复杂度已经超出本地科研 MVP 当前需求，停止
  继续开发和修复；其生产级安全验收状态为 `DEFERRED`。
- 未完成 Runner 实现的精确 8 路径 diff 已仓库外保存供未来参考，仓库内对应路径
  已恢复到当前 HEAD。该实现未暂存、未提交，也不构成当前正式入口或历史验收
  证据。
- 当前提交版本继续保留既有 Mock 回归、Chat Orchestration Harness 和公开入口的
  安全拒绝能力。不得通过手工拼装服务绕过既有 Provider、Runtime 和资源边界。

### 15.3 后续方向与本轮边界

- 后续优先推进智能体功能、Chat Orchestration Harness、Frontend 人工体验和多
  Tool 接入；正式 Runner 仅在未来需求重新成立后以新的独立工作单元评估。
- 本路线调整没有调用真实 Provider 或 Runtime，没有启动 GPU/模型，没有读取根
  `.env`，没有删除或修改 SEM、数据库 volume、MinIO volume 或用户已有数据。
- 路线调整和工作区恢复结果已经项目负责人验收通过。本轮只允许将阶段计划、当前
  进度和本报告精确纳入主题为 `docs: defer formal stage c runner` 的唯一收尾提交；
  提交后必须保持 working tree clean、staging empty、untracked 0，不得 push、
  amend、创建第二提交或恢复正式 Runner 开发。
