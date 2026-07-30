# Phase 1B 验收报告

## 一、恢复基线

- 报告初始工作单元：P1B2 门一——独立环境与依赖验证。
- 当前工作单元：P1B2 门三——真实 GPU 最小推理与资源测量。
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
