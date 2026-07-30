# 阶段 1 当前执行状态

> 本文件只保存当前动态状态、最近工作单元证据和下一步。产品与架构语义仍以五份已确认设计基线和阶段 1 实施计划为准。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 真实能力接入 |
| 当前里程碑 | P1B2 |
| 当前工作单元 | P1B2 门二：真实权重加载兼容性 |
| 状态 | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| 上一已验收工作单元 | P1B2 门二：真实权重加载兼容性 |
| Pre-M8 stop-loss commit | `891714dd58cf069073a7d4037be43ef66304d0ac` |
| M8 | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M8 acceptance commit | `18005944982ca5191412e06154effc67465ca3a7` |
| M9 | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M9 acceptance commit | `d7dee06c1f1294b010f64f5c532f5cb276ca53fa` |
| M10-A acceptance commit | `c597a89340072217d07d734e6b2ab88b6236d009` |
| M10-B acceptance commit | `f426e6f23703002a648991e5bb436929df19e8e2` |
| M10 overall acceptance commit | `f426e6f23703002a648991e5bb436929df19e8e2` |
| M11-A acceptance commit | `4ed740222238433541fb31c993dd75610634d157` |
| M11-B / Phase 1A acceptance commit | `f5e24dcaab4801dbeffb8400f2960c33b60b4f00` |
| M12-A acceptance commit | `ffd29353cb4682c74fd3455425822999444bb1d2` |
| P1B1 验收提交 baseline branch / HEAD | `main` / `61a6a88db674cbd84990d6b5288d8db8eec480ed` |
| P1B1 验收提交 baseline subject | `test: complete real DeepSeek provider acceptance` |
| P1B1 验收提交 baseline parent | `ffd29353cb4682c74fd3455425822999444bb1d2` |
| P1B2 门一恢复 baseline branch / HEAD | `main` / `7367b410f800b63efa9b9d4ba94095a3a45efb7a` |
| P1B2 门一恢复 baseline subject | `feat: add offline zta35g runtime` |
| P1B2 门一 acceptance commit | `c86c8eddfbb7a4b7354dd2299465cf352530623a` |
| 暂存区 | 验收提交完成后 `empty`；只允许精确 5 路径暂存一次 |
| P1B1 验收提交范围 | 精确 26 个 allowlist 路径；原 24 路径加 Phase 1A Runner 和 compatibility 共享授权门 |
| P1B2 门一范围 | 精确 8 个 allowlist 路径；2 个测试兼容性修订路径加 6 个收尾路径 |
| P1B2 当前 allowlist | 保留门一 8 路径并新增 `test_model_loading.py`，合计精确 9 路径 |
| 已确认设计基线 | 五份均未修改 |
| 历史 migration | `0001`–`0008` 均未修改；当前唯一 head/current 为 `0009_timeline_query_indexes` |
| `SEM/` | 未修改；前置和最终 `SEM_INTEGRITY_OK`；四份真实权重与完整 bundle 均仅在 CPU 受控加载，未执行推理 |
| Mock Runtime | 实现和协议未修改 |
| Real Provider calls | `6 observed LLMCalls`：5 次计划验收调用 + 1 次额外人工知识问答 |
| Commit | 仅允许本轮唯一验收提交；实际 hash 不在提交前预填，由 Git 创建后报告 |
| Push | `NO` |
| Amend | `NO` |
| Git 外部动作 | 只允许精确 5 路径 add 和唯一验收 commit；禁止 push、amend、第二提交、rebase、reset、restore、stash、clean 和切换分支 |
| M10-A | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M10-B | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M10 overall | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M11 | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M11-A | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M11-B | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| Phase 1A | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M12 | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M12-A | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| M12-B | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| P1B1 | `COMPLETE / PROJECT_OWNER_ACCEPTED` |
| P1B2 | 门一 `COMPLETE / PROJECT_OWNER_ACCEPTED`；门二 `COMPLETE / PROJECT_OWNER_ACCEPTED`；门三、门四 `NOT STARTED / NOT AUTHORIZED` |
| M13–M16 | `HISTORICAL DECOMPOSITION / MAPPED TO P1B1 AND P1B2` |
| M11 start baseline | `main@f426e6f23703002a648991e5bb436929df19e8e2` |
| M11-B start baseline | `main@4ed740222238433541fb31c993dd75610634d157` |
| M12-A start baseline | `main@f5e24dcaab4801dbeffb8400f2960c33b60b4f00` |
| M12-B start baseline | `main@ffd29353cb4682c74fd3455425822999444bb1d2` |
| 是否处于项目负责人暂停点 | 是，停在 P1B2 门二验收提交完成点；门三和门四未开始、未授权。 |
| 更新时间 | `2026-07-30` |

## P1B2 门一：独立环境与依赖验证

- 正式审计环境为 `materialsagent-zta35g` / Python `3.8.20`。项目负责人在首轮安装
  阻塞后手工完成候选依赖安装；Codex 本轮未创建、更新、删除或修复任何 Conda/Pip
  环境。
- 已只读核对 `torch==1.13.1+cu116`、`torchvision==0.14.1+cu116`、CUDA Runtime
  `11.6`、`torch.cuda.is_available() == True`、NVIDIA GeForce RTX 3060 Laptop
  GPU、Compute Capability `8.6`、`numpy==1.22.3`、`scipy==1.10.1`、
  `joblib==1.4.2`、`scikit-learn==1.0.2`、Pillow `10.4.0` 和
  `pytest==8.3.5`；目标环境 `pip check` 为 `No broken requirements found.`。
- 实际 GPU 为 RTX 3060 Laptop GPU、`6144 MiB`，并非旧候选 RTX 4060 / 8 GB。
  项目负责人既有极小 CUDA Tensor 证据为结果 `[2.0, 3.0]`、allocated
  `1024 bytes`、reserved `2097152 bytes` 和 `CUDA_BASIC_TEST_PASSED`；这只证明
  CUDA 基础可用，不是 DDPM、真实权重或真实模型验收。
- Python 3.8 修订前 RED 为 `124 passed, 3 skipped, 2 failed`：一项因
  `ast.unparse` 不存在，一项因解释器相关 `ast.dump` SHA-256 不一致。生产 Runtime
  无需修改。
- 仅修改两个单元测试：采样循环改为 Python 3.8 兼容的精确 AST 节点/字段断言；
  模型方法保护改为忽略格式 token 的语义 Token 摘要。14 个方法摘要先由 Python
  3.8 与 Python 3.11 只读脚本逐项确认完全一致，且继续保留 self 属性精确集合、
  禁止属性、`__init__` 默认参数和全部关键方法结构保护。
- GREEN 聚焦结果在 Python 3.8 和 Python 3.11 均为 `44 passed`；Runtime 全量在
  两边均为 `126 passed, 3 skipped`。精确三项 skip 均来自 P1B2 真实模型授权门：
  `test_minimal_inference.py`、`test_model_loading.py` 和
  `test_payload_and_resources.py`，不是依赖导入、路径、收集或生产代码问题。
- 两套解释器对 `zta35g-runtime/src` 的 `compileall -q` 均退出 0。生产 Runtime
  源码、Backend、Frontend、Mock Runtime、`SEM/`、五份设计基线和 migration 均未修改。
- `requirements-win-py38.lock.txt` 和 `materialsagent-zta35g.yml` 已将 torch /
  torchvision 的本机 wheel 路径规范化为精确官方 CUDA 11.6 HTTPS wheel
  direct references，并记录 SHA-256；使用 `packaging.requirements.Requirement`
  校验名称、URL 和哈希，使用目标环境 `importlib.metadata` 确认 torch /
  torchvision 版本及其余 22 个发行版。两文件均为 UTF-8/LF，不含本机绝对路径、
  `file:///`、editable 或 `-e` 记录。本轮没有删除或从零重建现有环境。
- 本轮真实 `torch.load` 权重调用 0、真实 `joblib.load` 权重调用 0、真实模型构造
  0、DDPM 采样 0、GPU 模型推理 0；真实 Runtime 未启动，Backend、MinIO 和
  DeepSeek 未调用，根 `.env` 未读取或修改。
- 结束环境保护审计中，Backend 87 包规范 JSON 指纹与开始值一致。Base 原始开始／
  结束包数组快照没有持久化，无法逐字段复原旧哈希差异；命令日志证明 `d53…` 与
  `d446…` 来自不同的数组处理和 JSON 序列化算法，同一算法下的开始／结束
  `d446…` 比较结果一致。
- Base 当前 Conda 语义投影连续三轮均为
  `7e04c35067f4d257351063091a2d64a79d75c9497f08bf6ff7f008782a3b6d70`
  （505 包），pip 语义投影连续三轮均为
  `69bb3db228283bee065c030f3060c8200dcc2fb20c8879d9e32342d14ab9a937`
  （437 包）。最后 Conda revision 为 `2025-11-14`，本轮无新 revision，
  `conda-meta/history` 和 package JSON 本轮无写入，也没有 Base 包身份变化证据。
  结论为 `BASE_PACKAGE_SET_UNCHANGED` / `AUDIT_FINGERPRINT_FALSE_POSITIVE`；
  此前阻塞属于跨实现审计指纹误报。
- 当前门一状态为 `COMPLETE / PROJECT_OWNER_ACCEPTED`；门一 acceptance commit 为
  `c86c8eddfbb7a4b7354dd2299465cf352530623a`。门二经本轮项目负责人授权执行后，
  状态为 `COMPLETE / PROJECT_OWNER_ACCEPTED`。

## P1B2 门二：真实权重加载兼容性

- 开始基线精确为 `main@c86c8eddfbb7a4b7354dd2299465cf352530623a`，
  subject `test: verify zta35g environment compatibility`，parent
  `7367b410f800b63efa9b9d4ba94095a3a45efb7a`；代码审查修订恢复门确认暂存区和
  未跟踪文件为空，工作区精确为项目负责人声明的 5 个 P1B2 门二路径。
- 主机物理内存的指定 CIM 查询被 Windows 拒绝访问；只读
  `GlobalMemoryStatusEx` 替代测量为总计 `15.40 GiB`；代码审查真实重跑前空闲
  `8.62 GiB`。RTX 3060 Laptop GPU 为 `6144 MiB`，
  加载前显存使用 `0 MiB`、计算进程 `0`，资源门通过。
- 前置 `SEM_INTEGRITY_OK` 为 57 文件、`2043071133` bytes、aggregate fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
  四个固定目标均为 manifest 唯一项，实际大小和 SHA-256 与 manifest 逐项一致。
- compatibility 测试仅在目标 Python 3.8 子进程内设置
  `ZTA35G_REAL_MODEL_ACCEPTANCE=P1B2_PROJECT_OWNER_AUTHORIZED`；GPU 授权保持未设置，
  命令结束后授权、环境名和模型根进程变量均恢复。
- DDPM 阶段完成 CPU `torch.load(map_location="cpu")`、正式 state 选择/清理、
  `ConditionalUNet` 构造、missing/unexpected/shape mismatch 全零检查和
  `load_state_dict(strict=False)`；模型与 checkpoint 随后释放，未调用 forward、
  sample 或 CUDA 迁移。checkpoint/model key 均为 271，参数元素为 `124946629`。
- DenseNet 的只读精确诊断确认转换后差异为 missing `121`、unexpected `0`、
  shape mismatch `0`；121 个 missing 全部且仅为启用 running stats 的 121 个
  BatchNorm 模块对应的 `.num_batches_tracked` 集合，没有其他参数或 buffer 差异。
- 全新 DenseNet121 和独立转换 state 随后真正执行
  `load_state_dict(strict=True)`；调用未抛错，返回 missing/unexpected 均为 0。
  PyTorch 1.13.1 内置旧 BatchNorm 版本兼容逻辑在模块内部 state 副本中精确补入
  121 个 counter，全部为 CPU `torch.int64` 标量 0；调用方 state 未被手工填充。
  生产 `ModelBundleLoader` 保持 strict=True，生产源码未修改。
- 两个固定 SVR 均受控加载为
  `sklearn.compose._target.TransformedTargetRegressor`，最终 estimator 均为
  `sklearn.svm._classes.SVR`。Yield Strength 的 Pipeline 步骤为
  `prep, svr` 和嵌套 `scaler, pca`，PCA components 为 5，支持向量 shape
  `[23, 9]`；Elongation 的 Pipeline 步骤为 `prep, svr`，无 PCA，支持向量
  shape `[21, 8]`。两者暴露的 `n_features_in_` 均为 3076，与正式输入契约一致。
  顶层类型、最终 estimator、全部 Pipeline、PCA 和 support vectors shape 均作为
  固定硬断言通过，两个安全摘要均为 `exact_identity=true`。
- 正式 `ModelBundleLoader(model_root).load()` 在 CPU 成功加载固定
  `zta35g-sem-original-bundle`；四项 load summary 均为 0/0。loader 为无状态工厂，
  为避免创建第二套完整对象未执行第二次 load。`LoadedModelBundle` 不提供
  `is_loaded()` API；已验证 close 前后内部 `_closed` 为 `false → true`，并在
  持有者释放后清理可验证弱引用。
- 代码审查修订后真实 compatibility 为 `22 passed in 8.30s`。清除授权后，目标
  Python 3.8 与 Backend Python 默认 Runtime 回归均为 `143 passed, 3 skipped`；
  两套 `compileall -q` 均通过。分阶段 SVR 身份检查代码未显式调用 predict、
  transform、fit 或 score；正式完整 bundle 加载路径由运行期 canary 保护，
  predict、transform、fit 和 score 计数均为 0。整个测试的 Module `__call__`、
  DDPM 动态类/DenseNet/Sequential 直接 forward、DDPM sample/采样循环和
  Module/Tensor CUDA 迁移计数均为 0。
- compatibility 负例使用小型 fake state/helper，覆盖精确 counter 集合、少一个、
  多一个伪造 counter、缺 running_mean、unexpected、shape mismatch 和 strict 抛错；
  SVR 负例覆盖错误类型、最终 estimator、Pipeline、PCA、support vectors shape
  和 3076 正确但类型错误；canary 自测试分别触发 module-call 与直接 forward。
  这些负例均不加载真实权重。Runtime 生产源码、Backend、Frontend、Mock Runtime、
  `SEM/`、环境/锁文件、五份设计基线和 migration 均未修改。

## P1B1 第一轮代码审查修订与验证证据

- 完整只读对照 `SEM/ZTA35G_lab/virtual_lab_sem.py` 后，DDPM 模型模块属性恢复为
  原始 `state_dict` 身份，并补回原定义的 `ConditionEmbeddings.emb_dim` 元属性；
  精确属性集合、默认参数与完整 `__init__`/`forward` AST 摘要锁定所有层构造、
  通道/卷积参数和数学顺序；未运行或 import 原文件，未建立大规模 key rename 表。
- DDPM mismatch 仅用 `strict=False` 采集计数，missing/unexpected 任一非零立即
  fail closed；DenseNet 使用 `strict=True`。失败关闭当前及已加载对象，错误不含
  私有键名或绝对路径，Runtime 不进入 ready。
- 生产采样删除 `torch.nan_to_num`；每个 DDPM 迭代后发现 NaN/Inf 立即
  `InvalidModelOutputError`，不继续 DenseNet/SVR，不返回图片或性能成功；逐迭代
  AST 门确认检查位于采样循环体最后一步且直接抛错。
- 四类加载失败（bundle 反序列化、DDPM device、DenseNet device、beta/alpha）
  均清空引用、禁止重复 load、保持 `engine.is_loaded()=false`；Runtime ready 为
  HTTP 503、NOT_READY、INVALID、device unavailable/unknown、bundle null。
- ready 加载成功空闲/执行中分别精确表达 AVAILABLE 与 busy；现有 Backend
  Adapter 对 busy 为 DEGRADED、对非 200 ready 为 UNAVAILABLE，Backend 生产代码
  未修改。
- 新增标准库安全 JSON 日志，独立 Runtime 入口显式配置专用 INFO handler，覆盖
  启动、加载、加载失败、接收、busy、SEM、性能、完成、执行失败与停止；干净子进程
  和捕获测试确认默认可输出，且不含 Token、Base64、私有路径和完整异常正文。
- compatibility 三条真实测试共同要求精确 P1B2 授权、模型 Conda 环境和非空模型
  根；真实推理/资源测试额外要求 GPU 授权。默认结果为 `1 passed, 3 skipped`，
  跳过发生在 Torch/Joblib import 或权重打开之前，本轮未设置任何授权变量。
- 次要合同已收紧：`0 <= seed < 2**63`、递归 JSON 映射 HTTP 400，懒导入由
  干净 Python 子进程证明。
- 审查聚焦测试 `104 passed`；Runtime 全量 `107 passed, 3 skipped`；Mock
  Runtime `11 passed`；Frontend `204 passed`，typecheck/build 通过。
- Phase 1A Runner 新增默认 M12A 的 `ScopeMilestone` 参数，P1B1 pre/post scope
  命令与日志表达实际 milestone；历史 M12A allowlist 未修改。权威 run
  `20260729T151355Z-a3a7b642f201` 为 `PHASE_1A_AUTOMATION_PASSED`、
  `failed=0`：M11 E2E `24 passed`、Backend `985 passed`、Mock Runtime
  `11 passed`、Frontend `204 passed`，pip/compileall/SEM/Git/安全扫描和受控
  Mock Stack 清理均通过。
- 直接无受控栈 Backend 全量达到 10 分钟工具上限且无断言输出；只清理了该命令
  的精确 Conda/pytest PID 树。权威 Backend 全量证据采用上述 Runner 结果。
- 当前 P1B1 检查为 `SCOPE_OK P1B1`；`SEM_INTEGRITY_OK`；暂存区为空。
- 修订后只读复核未发现剩余 Critical、Important 或 Minor，assessment 为
  `APPROVED`；该结论仅覆盖本次 P1B1 代码复核，仍停在项目负责人复审点。

## P1B1 第二轮代码审查修订与验证证据

- Runtime 关闭先原子标记 closed 并记录安全 `runtime_stopping`，再等待
  `execution_lock`；当前 execute 完整形成响应后才关闭 Engine，最后关闭 HTTP
  Server。关闭期间的新请求在读 body 前、解析后及取得执行锁后均 fail closed 为
  安全 503，不开始模型执行、不增加 execution count；重复 close 仍只关闭 Engine 一次。
- compatibility 公共门现在核对 `sys.version_info[:2] == (3, 8)`，Python 3.11、
  3.9 以及环境名正确但版本错误均在任何 Torch/Joblib import 或权重打开前失败。
  普通门通过且 GPU 已授权后才 import Torch；CUDA 不可用明确 fail，不能 skip 或
  回退 CPU。真实推理/资源测试还要求 `engine.device_kind == "cuda"`。
- 固定 bundle 身份精确为 `zta35g-sem-original-bundle`。Runtime 启动只在
  loaded=true、固定 bundle 且 device 为 cpu/cuda 时进入 ready；任一不变量失败都
  立即且仅一次关闭 Engine，ready 为 503、bundle null、`MODEL_LOAD_FAILED`。
  execute response 的空、其他、带空白或超长 bundle 均安全映射 500；response
  完整校验前不记录诊断，校验后日志只使用固定 bundle 和受限 device 值。
- RecursionError 契约测试改为 monkeypatch `contracts.json.loads` 确定性抛错；
  不再依赖 2000 层 JSON 在不同解释器中的递归行为。
- 四组 RED/GREEN：关闭一致性 `2 failed → 2 passed`；Python/GPU 授权门
  `4 failed, 1 skipped → 4 passed, 1 skipped`；固定 bundle
  `6 failed, 7 passed → 13 passed`；RecursionError 捕获 mutation
  `1 failed → 1 passed`。只读复核追加的运行期 bundle 日志 canary
  `1 failed, 1 passed → 2 passed`；关闭测试同时捕获线程异常并断言 close 正常返回。
- 第二轮聚焦组合 `110 passed, 1 skipped`；Runtime 全量
  `126 passed, 3 skipped`；隔离 compatibility
  `4 passed, 3 skipped`、`HEAVY_IMPORTS=[]`；Mock Runtime `11 passed`；
  Frontend 9 files / `204 passed`，typecheck/build 通过。
- 正式 Phase 1A Runner
  `20260729T163241Z-d7841be818ab` 为
  `PHASE_1A_AUTOMATION_PASSED`、`failed=0`：M11 E2E `24 passed`、Backend
  `985 passed`、Mock Runtime `11 passed`、Frontend `204 passed`，
  pip/compileall、P1B1 pre/post scope、SEM、Git 与动态安全扫描均通过。
  Runner 结束后 3000/8000/8100 无监听、Compose 运行服务为 0、状态文件已删除。
- 直接无受控栈 Backend 全量约 4 分钟仍无断言输出；在核对 ownership 后只停止该
  命令精确的 Conda/pytest PID 树。权威 Backend 全量采用上述受控 Runner 结果。
- 当前仍为精确 26 个 P1B1 allowlist 路径，暂存区为空；四份真实权重打开、
  Torch/Joblib 真实 loader、真实模型、GPU 和 DeepSeek 调用均为 0。P1B2 未开始。
- 日志 canary 与关闭线程断言修订后的只读复核为 Critical 0、Important 0、
  Minor 0、`APPROVED`；这不替代项目负责人最终代码复审。

## M8 已实现内容

### IdempotencyRecord 与四种 operation

- 新增 migration `0008_idempotency_record`，`down_revision = 0007_tool_result_explanation`；以 `(actor_id, operation, idempotency_key)` 唯一约束裁决同 Actor、同操作、同 key 的首次请求。
- 四种 operation 精确为 `TASK_CREATE`、`TASK_INPUT_SUPPLEMENT`、`TOOL_RETRY`、`EXPLANATION_RETRY`。记录固定保存请求 digest，并按 operation 绑定 Task、Message、Revision、ToolRun 或 Explanation 的既成事实。
- Idempotency-Key 必须为 1–255 字符、非空、无 Unicode control character；key 保持不透明，不做大小写折叠或空白规范化。
- digest 使用规范 JSON：对象 key 排序、UTF-8、无多余空白、禁止 NaN，再计算 SHA-256。资源、正文、submission mode、target task、retry reason、language 等 operation 输入全部进入相应 digest。
- 同 key 同 digest 只返回当前数据库投影，`idempotency_replayed=true`，不重新调用 Chat、Runtime、MinIO 或 Explanation Provider；同 key 异 digest 固定返回 409 `IDEMPOTENCY_CONFLICT`。
- 内部幂等预留结果明确区分 `CREATED`、`RECOVERED_OWN_COMMIT` 和 `REPLAY`。只有另一 HTTP 请求命中既有记录才把公共 `idempotency_replayed` 设为 true；当前请求在 commit uncertainty 后重查到 `first_request_id` 等于自身 request_id 时继续原链路，公共 replay 保持 false。
- 短事务 commit 结果不确定时，以新 UoW 按唯一 scope 重查已提交记录及其完整资源绑定；自己的已提交预留继续后续外部调用且只调用一次，另一请求 replay 只读取数据库事实。
- M8 不实现过期、清理或 key 复用：`expires_at` 为 nullable，当前所有新记录写 `NULL`，migration 不含 expiry 顺序约束和 expires 索引。

### TASK_CREATE 与 TASK_INPUT_SUPPLEMENT

- 消息提交公共 API 强制要求 `Idempotency-Key`。首次 `TASK_CREATE` 在一个短事务内写入 Task、User Message 和 IdempotencyRecord；Chat Provider 在事务外运行。
- 同 Actor 的消息幂等预留先锁定 Actor 行，使 20 路同 key 请求在 PostgreSQL 中顺序完成唯一裁决；唯一约束仍是最终一致性防线。
- 回放投影在读取 Task 时使用 `FOR UPDATE`，避免在 Chat finalize 同时提交时跨多个 `READ COMMITTED` statement 拼接出混合快照。
- Supplement 只允许当前 Actor、同 Conversation、`NEEDS_INPUT` 且无活动运行的 Task；锁定 Task 后先拒绝该 Task 上另一条未绑定 Revision 的 Supplement record 或 PENDING/RUNNING Chat LLMCall，再写入新的 User Message 和幂等记录。不同 key 固定返回 409 `TARGET_TASK_NOT_RECOVERABLE`，Provider 在事务外调用，再写完整下一版 Revision。
- Supplement Revision 的 `source_message_ids` 覆盖该 Task 的完整用户消息链；成功补参会重新验证完整输入并只执行一次 Tool 链，回放不重复执行。

### TOOL_RETRY

- 新增 `POST /api/v1/tasks/{task_id}/tool-runs`，默认 reason 为 `USER_REQUESTED_RETRY`，成功和回放均返回稳定业务投影及 `idempotency_replayed`。
- 预留事务锁定 Task，重验 Actor、Conversation、当前 Revision、selected references、资格与无活动 attempt；按现有最大 attempt 加一，生成未使用的新 seed。
- 新 ToolRun PENDING、IdempotencyRecord 和 Task RUNNING 在同一短事务提交；Runtime execute 在 UoW 外且不自动重试。
- Runtime 成功后，Asset 生命周期仍为 PENDING → 事务外 decode/MinIO put+HEAD → AVAILABLE；Result commit 使用 retry 专用来源校验，在写 Result/Link 前按 Task 锁、新 ToolRun 锁、旧 selected ToolRun/Result 锁顺序重验旧来源、状态/输出集合、新 attempt 单调性和唯一活动 retry，再在同一短事务写 Result/Link、终结新 ToolRun并替换 Task selected references。
- 旧 ToolRun、Asset、Result 和 Explanation 历史保持可读；回放 key 不新增 Runtime execute、Storage put、ToolRun、Asset、Result 或 Explanation。
- `NEEDS_INPUT`、知识问答、硬校验失败、已成功、仅 Explanation 失败、已有活动 attempt 等不可重试状态均被拒绝；失败/部分成功及无先前 ToolRun 的受控 `TOOL_UNAVAILABLE` 失败可按完整有效 Revision 重试。

### EXPLANATION_RETRY

- 新增 `POST /api/v1/tool-results/{result_id}/explanations`；默认 language 为 `zh-CN`、reason 为 `USER_REQUESTED_RETRY`，成功和回放返回稳定业务投影及 `idempotency_replayed`。
- 预留事务按 Task → Result 顺序加锁，重验 Actor、Result、selected Result、既有 Explanation 资格；按该 Result 最大 attempt 加一。
- 新 LLMCall、PENDING Explanation、IdempotencyRecord 与 Task RUNNING 在同一短事务提交；HTTP request_id 作为新 attempt 的 request_id；Explanation Provider 在 UoW 外且只调用一次。
- finalize 复用受控安全失败映射；成功 Explanation 不改变 Result，FAILED Result 也不会因 Explanation 成功而被提升为成功 Task。
- Result 查询优先返回最新成功 Explanation；如果后续 attempt 失败，仍保留最近成功正文，并通过 `latest_explanation_failure` 暴露固定安全失败摘要；若从未成功，则返回最新终态失败。
- Tool retry 投影复用同一 Explanation attempt 选择函数，不再自行按最大 attempt 覆盖最近成功 Explanation；最近失败 attempt 仍单独表达。
- 相同 key 改变 language 或 reason 返回 409；新 key 使用不支持的 language 返回输入校验错误。

## 事务与外部调用边界

| 操作 | 锁定与写入 | commit / rollback | 外部调用 |
|---|---|---|---|
| TASK_CREATE 预留 | Actor；写 Task、User Message、IdempotencyRecord | 三者同一短事务提交；任一失败整体回滚 | Chat Provider 在提交后、UoW 外 |
| TASK_INPUT_SUPPLEMENT 预留 | Actor、目标 Task；写 User Message、IdempotencyRecord | 同一短事务提交；失败整体回滚 | Chat Provider 在提交后、UoW 外 |
| Chat finalize | Task；写 LLMCall 终态、Assistant Message/Revision、Task 聚合及 supplement record 的 Revision 绑定 | 单一短事务提交；失败回滚，回放只读既成事实 | 无 |
| TOOL_RETRY 预留 | Task；写新 ToolRun、IdempotencyRecord、Task RUNNING | 同一短事务提交；失败整体回滚 | Runtime execute 在提交后、UoW 外 |
| Asset 生命周期 | 分离的 PENDING 与 AVAILABLE/失败短事务 | DB commit 与对象存储不组成长事务；保留既有补偿语义 | decode、PNG、MinIO put/HEAD 在 UoW 外 |
| retry Result commit | Task、ToolRun、Revision、Asset 来源；写 Result/Link，终结 ToolRun，替换 Task selected references | 单一短事务原子提交；冲突或持久化失败回滚 | 无 |
| EXPLANATION_RETRY 预留 | Task、Result；写 LLMCall、Explanation、IdempotencyRecord、Task RUNNING | 同一短事务提交；失败整体回滚 | Explanation Provider 在提交后、UoW 外 |
| Explanation finalize | Task、Result、LLMCall、Explanation；写终态并聚合 Task | 单一短事务提交；冲突时回滚并以新 UoW 验证等价终态 | 无 |

Runtime、MinIO 和 Explanation Provider 调用均不在数据库 UoW 内。Backend 不自动重试 Runtime；显式 Tool retry 总是创建新 ToolRun 和新 seed。

## M8 第一轮审查修订证据

- 四类 own-reservation commit uncertainty 均以 commit 实际成功后抛 `PersistenceError` 注入：TASK_CREATE 最终 `SUCCEEDED`、Chat Provider 增量 1；SUPPLEMENT 最终 `NEEDS_INPUT`、Chat Provider 增量 1；TOOL_RETRY 最终 `SUCCEEDED`、Runtime 增量 1、Explanation 增量 1；EXPLANATION_RETRY 最终 `SUCCEEDED`、Provider 增量 1。四个原请求公共 `idempotency_replayed=false`，后续真实第二请求为 true。
- Chat `_prepare_call`、Chat `_start_call`、Tool `_start_pending_attempt`、Explanation `_start` 的实际成功后报错路径均使用新 UoW 重查。三个 start 故障注入场景的 Chat Provider、Runtime、Explanation Provider 增量分别都精确为 1，最终均为稳定 `SUCCEEDED`；没有第二次相同状态写入。
- 真实 PostgreSQL 受控并发：同一 `NEEDS_INPUT` Task、两个不同 key 和不同正文同时补充，首请求 200、第二请求 409 `TARGET_TASK_NOT_RECOVERABLE`；只新增 1 User Message、1 Supplement IdempotencyRecord、1 Chat LLMCall，Provider 增量 1，PENDING/RUNNING orphan Chat LLMCall 为 0。原 20 路同 key 用例保留并通过。
- RETRY 旧 selected references 的四种损坏注入全部在新 Result/Link 写入前被拒绝：Result 来自另一 Run、Run 来自另一 Task、result-only、旧 Run/Result 状态不一致；每种场景新 Result/Link 均为 0，旧 Task/Run/Result/Link 不变。
- `expires_at=NULL` 通过 Domain、Repository、migration schema 与 `0008 → 0007 → 0008` 往返；无 expiry check、无 expires index、无清理器或过期 key 复用。
- Tool retry 在“后续较大 attempt 为 FAILED、已有较早 SUCCEEDED Explanation”场景继续投影最近成功 Explanation；共享选择规则测试通过。

## M8 第二轮审查修订证据

### 同步 Tool 完整链的不确定提交恢复

- 初始 ToolRun PENDING 创建在 commit 报 `PersistenceError` 后，以全新 UoW 按预生成 `tool_run_id` 重读；仅当 Task、request、Revision、attempt、Tool/version/schema、requested outputs、execution input、`PENDING` 状态和 `created_at` 全部等价时继续 start 与 Runtime。测试最终只存在 1 ToolRun，Runtime、Storage put、Explanation 各调用 1 次，Task 为 `SUCCEEDED`，没有生成第二个 seed。
- Runtime success 在提交前构造完整 `recorded` RUNNING ToolRun；不确定提交后重验 attempt identity、`actual_runtime_parameters`、diagnostics、output summary、model bundle 及其余完整字段。初始链和 retry 链均继续 Asset/Result/Explanation；每条链 Runtime 只调用 1 次。
- Runtime failure 区分三种 fresh-read 结果：等价 FAILED 事实视为已提交并继续抛 `ToolExecutionOutcomeError`；仍等于提交前 RUNNING 事实视为真实持久化失败；其他混合或不一致事实为 409 冲突。完整 workflow 随后选择该失败 ToolRun，Task 最终 `FAILED`、`selected_result_id=NULL`，Runtime 只调用 1 次。
- Result 原子事务预先保留期望 Result、Links、终态 ToolRun、聚合 Task 与来源 Assets；不确定提交后按预生成 `result_id` 重读并严格核对 Result 全字段、Link 数量/asset/order、AVAILABLE 同 Run Assets、ToolRun 终态以及 Task selected references/status/error。initial 与 retry 均只保留 1 份本次 Result 并继续 Explanation attempt 1；旧“Result 已提交但 Task RUNNING、Explanation 0 次”测试已改为最终稳定 Task 与 Provider 1 次。
- 初始 Explanation prepare 在 commit uncertainty 后按预生成 `explanation_id` 与 `llm_call_id` 重读，严格核对同 Task/Result、attempt 1、ToolRun request_id、两者 PENDING、provider/model/template/digest/parameters/language 与 Explanation projection，等价时继续 start 和 Provider。
- Explanation finalize 对 initial 和 retry 均在 commit uncertainty 后调用 fresh-read 等价终态校验；完全等价返回持久化 Explanation，仍为原 RUNNING 事实保留既有 `EXPLANATION_PERSISTENCE_FAILED`，混合或不一致终态返回安全冲突。Provider 均只调用 1 次。
- Chat success finalize 在 commit uncertainty 后通过既有 projection/source identity/terminal consistency 校验，并额外核对本次知识答案或 Tool/NEEDS_INPUT 的 summary 与 Revision payload；等价时返回当前 projection。Chat failure finalize 重读并核对相同 error code/message 的 FAILED LLMCall 与 Task，使 HTTP 继续返回原持久化 outcome error；Chat Provider 不重复调用。

### selected references 全空历史损坏

- `ToolExecutionService.reserve_retry_attempt` 的 `failed_before_run` 现在同时要求 `runs == []`；已有旧 ToolRun/Result 但故障注入清空 Task 两个 selected 字段时，固定 409 `TASK_NOT_RETRYABLE`，Runtime、Storage、Explanation 与行数均不增加。
- `ResultService._validate_retry_selection` 在 selected 两项均为空时只允许 `prior_runs == []` 且当前 retry attempt 为 1；绕过 reservation 直接提交 retry Result 的 BOTH_NULL 故障测试在任何新 Result/Link 写入前返回 `ApplicationConflictError`，旧 Task/Run/Result/Link 保持不变。

## M8 最终核心复审聚焦修订证据

### Tool retry reservation 完整 selected 事实校验

- `failed_without_result` 现在显式要求 Task 已选择 FAILED ToolRun、`selected_result_id=NULL`、该 Run 属于当前 Task，且按 Run 重查确实不存在 ToolResult；`selected_result_id` 非空但按 ID 读不到 Result 不再被当成“无 Result”。
- `failed_or_partial_result` 在新 ToolRun、TOOL_RETRY IdempotencyRecord 和 Task RUNNING 写入前，逐项核对 Task selected IDs、Result Actor/Task/Run 来源、Run/Result 三组 requested/completed/failed outputs，以及 `FAILED → FAILED → FAILED` 或 `PARTIALLY_SUCCEEDED → PARTIALLY_SUCCEEDED → PARTIALLY_SUCCEEDED` 的严格 Task/Result/Run 状态链。
- 四种受控损坏——selected Result 读不到、selected Result 指向另一个 Run、selected Run/Result 状态不一致、selected Run/Result output 集合不一致——均返回 HTTP 409 `TASK_NOT_RETRYABLE`。每种场景 ToolRun 与 TOOL_RETRY IdempotencyRecord 行数不增加，Runtime/Storage put/Explanation 调用增量均为 0，Task 状态和 selected references 不变。
- `ResultService._validate_retry_selection()` 及既有五类 Result corruption 测试保持不变，继续作为 Result commit 前的第二道防线。

### Chat NEEDS_INPUT finalize 恢复等价

- NEEDS_INPUT 不确定提交恢复现在要求已持久化 AssistantMessage 存在且 `content_text` 精确等于固定 `FOLLOW_UP_TEXT`；精确正文恢复返回原 `NEEDS_INPUT` 投影，任何不同正文固定返回 409 `RESOURCE_CONFLICT`，Chat Provider 不重复调用。
- 同一分支将期望 summary 的 missing/ambiguous 集合按 LLMCall 领域模型的冻结 tuple 形式比较，避免正确持久化事实因 list/tuple 表示差异被误拒绝。

## 并发与故障恢复证据

- `20 concurrent TASK_CREATE`（知识问答）：只形成 1 Task、1 User Message、1 IdempotencyRecord 和 1 Chat Provider call；19 个响应为 replay。
- `20 concurrent TASK_CREATE`（完整 Tool）：只形成 1 Task、1 User Message、1 Revision、1 ToolRun、1 Asset、1 Result 和 1 IdempotencyRecord；Chat、Runtime、Storage put、Explanation 各 1 次。执行未完成时 replay 可返回尚无 selected references 的当前事实，所有非空 selected references 一致。
- `20 concurrent SUPPLEMENT`：只新增 1 User Message、1 Assistant Message、1 Revision、1 IdempotencyRecord 和 1 Chat Provider call；19 个响应为 replay。
- `20 concurrent TOOL_RETRY`：只新增 1 attempt、1 Runtime execute、1 Storage put、1 Result/Explanation 链；19 个响应为 replay。
- `20 concurrent EXPLANATION_RETRY`：只新增 1 Explanation attempt、1 LLMCall 和 1 Explanation Provider call；Runtime 与 Storage 调用为 0；19 个响应为 replay。
- TASK_CREATE 与 SUPPLEMENT 的两个 20 路并发用例连续重复 10 轮，每轮 `2 passed`，未再出现 PostgreSQL 唯一冲突链或混合快照 409。
- 消息预留 commit uncertainty：若重查到本 request_id 已提交的 record，分类为 `RECOVERED_OWN_COMMIT` 并继续原链路；若是另一 request_id 才分类为 `REPLAY` 并禁止外部调用。
- Tool/Explanation 预留、Chat/Tool/Explanation start、Result/Explanation finalize 的不确定提交和失败路径均由故障注入覆盖；只认可新 UoW 查到的等价既成事实。
- Runtime timeout/unavailable/protocol/业务失败与 Explanation timeout/provider/protocol/非受控普通异常均形成固定安全终态；回放不重复外部调用。

## 自动化验证

- 最终核心复审新增 6 个参数化 case：生产修改前 Tool retry 为 `3 failed, 1 passed`，Chat NEEDS_INPUT 为 `1 failed, 1 passed`；生产修改后合计 `6 passed in 3.91s`。
- 最终核心三文件聚焦套件（M8 Tool retry、M8 message idempotency、Result commit）：`69 passed in 33.69s`。
- 第二轮新增/改写的 13 个回归用例在生产修改前为 `13 failed in 8.38s`；失败分别对应 Chat finalize、初始 ToolRun、Runtime success/failure、initial/retry Result、initial Explanation prepare/finalize、retry Explanation finalize 以及 selected BOTH_NULL 两条路径。生产修改后同组为 `13 passed in 9.11s`。
- 第二轮直接受影响的 M8 message/tool/explanation、Explanation outcomes、Message workflow、Result 与 Explanation unit 聚焦套件：`90 passed in 49.13s`。
- Backend 完整套件：`771 passed in 101.92s`，0 failed、0 skipped。
- 并发消息重复压力：10 轮，每轮 `2 passed`；总计 20 次 pytest case execution。
- 完整 Tool TASK_CREATE 20 路并发专项连续 3 轮：每轮 `1 passed`。
- Mock Runtime：`11 passed in 1.13s`。
- Backend 环境 `pip check`：`No broken requirements found.`。
- `python -m compileall -q backend/src mock-runtime/src`：exit 0。
- 当前本地 PostgreSQL：唯一 head/current 为 `0008_idempotency_record (head)`；`alembic check` 为 `No new upgrade operations detected.`。
- nullable expiry 迁移往返：`upgrade head → downgrade 0007_tool_result_explanation → upgrade head` 成功；自动化也覆盖 `expires_at=NULL` 的 0008 downgrade-only round trip。
- `check-scope.ps1 -Milestone M8`：`SCOPE_OK M8`。
- `check-sem-integrity.ps1`：`SEM_INTEGRITY_OK`，57 files，`total_size_bytes=2043071133`，fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- `git diff --check`：通过。

## 真实 HTTP / PostgreSQL / MinIO / Mock Runtime 人工验收

使用随机一次性 PostgreSQL 数据库、真实已配置 MinIO bucket、仓库 Mock Runtime loopback HTTP 服务、Backend TestClient 公共 HTTP 路由和 Mock Explanation Adapter。

| 场景 | HTTP / 状态 | 幂等与引用 | Adapter 调用变化 | 数据库事实 |
|---|---|---|---|---|
| A NEW_TASK replay | 200 → 200，Task SUCCEEDED | 同 Task/Result，replay=true | Runtime +0，Storage put +0 | 1 Task/Run/Asset/Result/Explanation/record |
| B 同 key 异内容 | 409 `IDEMPOTENCY_CONFLICT` | 原资源不变 | 全部 +0 | Task/Message/ToolRun 等计数不变 |
| C Supplement replay | 200 → 200，Task NEEDS_INPUT | replay=true | replay 时 Provider +0 | 首次只增 1 User Message、1 Assistant Message、1 Revision、1 record；replay +0 |
| D Tool retry | 200，Task SUCCEEDED | attempt 1 → 2，新 seed、新 Run/Asset/Result；selected references 指向新结果；旧 Result GET 200 | Runtime HTTP +1，Storage put +1 | 两个 attempt 与两个不同 seed 均保留 |
| E Tool retry replay | 200 | 同 tool_run_id，replay=true | Runtime +0，Storage put +0 | 所有行数不变 |
| F Explanation retry | 200，Task SUCCEEDED | attempt 1 FAILED → attempt 2 SUCCEEDED；新 LLMCall/Explanation、同 Result，replay=false | Explanation +1，Runtime +0，Storage +0 | 该 Result 保留 2 个 Explanation attempt |
| G Explanation conflict | 409 `IDEMPOTENCY_CONFLICT` | 相同 key 改 reason | 全部 +0 | 所有行数不变 |
| H 所有权隔离 | GET Task/Result 与两个 retry 均 404 | 统一 `RESOURCE_NOT_FOUND` | Runtime/Storage/Explanation 全部 +0 | 所有行数不变 |

本次验收总计 Chat Provider 5 次、Mock Runtime HTTP execute 3 次、MinIO put 4 次、Explanation Provider 5 次。结束后删除本轮 4 个 MinIO 对象、丢弃一次性数据库并停止 Runtime；未删除命名 volume，未使用 `docker compose down -v`。

## M8 实际修改文件

### 生产代码

- `backend/src/materialsagent/api/dependencies.py`
- `backend/src/materialsagent/api/routes/conversations.py`
- `backend/src/materialsagent/api/routes/tasks.py`
- `backend/src/materialsagent/api/routes/tool_results.py`
- `backend/src/materialsagent/application/chat_orchestration.py`
- `backend/src/materialsagent/application/errors.py`
- `backend/src/materialsagent/application/explanation_service.py`
- `backend/src/materialsagent/application/idempotency.py`
- `backend/src/materialsagent/application/messages.py`
- `backend/src/materialsagent/application/result_service.py`
- `backend/src/materialsagent/application/retries.py`
- `backend/src/materialsagent/application/tool_execution.py`
- `backend/src/materialsagent/application/tool_workflow.py`
- `backend/src/materialsagent/domain/models/idempotency_record.py`
- `backend/src/materialsagent/domain/ports/unit_of_work.py`
- `backend/src/materialsagent/infrastructure/db/actor.py`
- `backend/src/materialsagent/infrastructure/db/conversation_task.py`
- `backend/src/materialsagent/infrastructure/db/idempotency_record.py`
- `backend/src/materialsagent/infrastructure/db/tool_result.py`
- `backend/src/materialsagent/infrastructure/db/unit_of_work.py`
- `backend/src/materialsagent/main.py`

### Migration

- `backend/alembic/env.py`
- `backend/alembic/versions/0008_create_idempotency_record.py`

### 测试

- `backend/tests/api/test_assets.py`
- `backend/tests/api/test_conversations.py`
- `backend/tests/api/test_explanation_outcomes.py`
- `backend/tests/api/test_m8_explanation_retry.py`
- `backend/tests/api/test_m8_message_idempotency.py`
- `backend/tests/api/test_m8_tool_retry.py`
- `backend/tests/api/test_message_orchestration.py`
- `backend/tests/api/test_tasks.py`
- `backend/tests/api/test_tools.py`
- `backend/tests/integration/db/test_idempotency_persistence.py`
- `backend/tests/integration/db/test_migrations.py`
- `backend/tests/integration/db/test_result_commit.py`
- `backend/tests/unit/test_explanation_service.py`
- `backend/tests/unit/test_idempotency.py`

### 脚本与动态状态

- `scripts/dev/check-scope.ps1`
- `docs/progress/phase-1-current-status.md`

以上 39 个路径全部属于 M8 allowlist；暂存区为空。五份设计基线、`0001`–`0007`、Mock Runtime 和 `SEM/` 均未修改。

## 已知风险

- PostgreSQL 与 MinIO 之间仍不是分布式事务；M8 沿用 M6 已验收的 PENDING、HEAD 验证、补偿删除和 ORPHANED 恢复边界，没有扩大该风险。
- IdempotencyRecord 目前不设置自动过期或清理策略；阶段 1A 本地 MVP 会持续保存这些审计/回放锚点。引入清理必须作为后续明确设计，而不是在 M8 内隐式删除。
- 回放返回“当前稳定业务投影”而不是逐字节重放历史 HTTP body，因此 task/explanation 的后续合法状态变化会反映在同 key 回放中；资源绑定 ID 保持不变。

## M9 已验收执行记录

- M9-P 基线 gate：`main@18005944982ca5191412e06154effc67465ca3a7`，暂存区与工作区均为空，`git diff --check` 与 `git diff --cached --check` 通过。
- Docker：PostgreSQL 与 MinIO 均为 healthy；命名 volumes 为 `materialsagent_postgresql_data` 与 `materialsagent_minio_data`，未删除。
- M9-P 已把本次“同一对话连续完成 M9-P、M9-A、内部 gate、M9-B 与全量验证”的负责人例外、各段边界、验收命令和精确 allowlist 写入阶段 1 计划与 `check-scope.ps1`。
- M9-A 新增版本 1 的 HMAC-SHA256 不透明 cursor、可选 `SecretStr` 签名密钥、Timeline Query Port/SQLAlchemy Adapter、数据库 keyset 与 `limit + 1`、固定批量查询及 `REPEATABLE READ READ ONLY` 快照。
- cursor 使用规范 JSON、base64url 和常量时间签名比较，绑定 Conversation，并限制 token 最大 2048 个字符、解码后的 canonical JSON payload 最大 512 bytes；完整保留数据库 UTC 微秒精度。密钥缺失只使 Timeline 返回安全 503。
- Alembic：新增 `0009_timeline_query_indexes`，`down_revision=0008_idempotency_record`；唯一 head/current 为 `0009_timeline_query_indexes`，`alembic check` 无待生成操作，`0009 → 0008 → 0009` 往返成功。
- 索引为 `ix_message_conversation_created(conversation_id, created_at, message_id)` 与 `ix_task_conversation_created(conversation_id, created_at, task_id)`。
- M9-A 内部 gate 在开始路由前通过：基线、allowlist、cursor/config/sort/query、固定 9 条 SELECT、只读可重复读、migration 往返、scope 与 diff checks 均满足。
- M9-B 新增 `GET /api/v1/conversations/{conversation_id}/timeline`：顶层只有 `USER_MESSAGE`、`ASSISTANT_MESSAGE`、`TOOL_TASK`；Tool 初始用户消息折叠进卡片，不在顶层重复。
- 顶层顺序固定为 `(anchor_at, item_type_rank, item_id)`，rank 为 10/20/30；Tool anchor 取最早用户消息，缺失时安全回退 Task.created_at。Timeline Tool 卡片只返回 `attempt_count`、`selected_tool_run`、`has_history`，并投影 selected Result/Assets/Explanation 和最多 50 条最近输入；完整 ToolRun 历史由 Task GET 返回。
- Task GET 通过短 `REPEATABLE READ READ ONLY` 事务的一致快照投影稳定 anchor、输入、全部 ToolRun、selected Result/Assets/Explanation、needs-input 与安全失败；Timeline 与 Task GET 都执行 Actor 所有权和 selected 来源一致性校验，不调用 Chat、Runtime、MinIO 或 Explanation Provider。
- 数据库 adapter 对 1 个和 20 个 Tool Task 均固定执行 9 条 SELECT，没有 N+1；并发插入测试证明一次响应保持单个可重复读快照，下次请求才看到新事实。
- 五份确认设计基线、`SEM/`、Mock Runtime 均未修改；未加载或运行真实模型。

## M9 第一轮统一代码审查修订证据

- Task GET 已从普通 READ COMMITTED UoW 多次读取改为专用 `TaskDetailQuerySnapshot`：一次 Service 调用只进入一次 query port，并在单个短 `REPEATABLE READ READ ONLY` 事务中读取完整 Task detail。
- Task、Messages、Revisions、ToolRuns、selected ToolResult、ResultAssetLink JOIN Asset、Explanation JOIN LLMCall 使用固定 7 条业务 SELECT 批量读取；1 attempt / 1 Asset / 1 Explanation 与 20 attempts / 8 Assets / 20 Explanations 均为 7 条业务 SELECT、10 条总语句，不随资源数量增长。
- Tool retry 并发测试在第一批查询建立快照后，由另一连接提交新 ToolRun、Result、Asset 和 selected references：当前响应完整保持旧链，下一次查询才完整看到新链和两次 ToolRun 历史。
- Explanation retry 并发测试在第一批查询建立快照后，由另一连接提交新 LLMCall、Explanation 和 Task 聚合状态：当前响应保持旧 Task/Explanation，下一次查询才同时看到新 Task 状态和成功 Explanation。
- 查询 Session 使用上下文管理器显式关闭；transaction 在成功、未找到和异常路径均回滚结束，connection 在 `finally` 中关闭。Session close 回归测试覆盖成功、未找到和异常三条路径。
- Task GET 公共字段、完整 ToolRun 历史、selected Result/Asset/Explanation、来源损坏安全 500、其他 Actor/缺失资源统一 404、数据库 unavailable 安全 503 均保持；缺少 Timeline signing key 时 Task GET 200、Timeline 503。
- 第一轮审查结论：Original Critical 0；Original Important 1 FIXED；Original Important 2 FIXED；Session close Minor FIXED；Route coupling Minor DEFERRED。

## M9 自动化验证

- Task query 单元：`6 passed in 0.20s`。
- Task detail/Timeline DB：`8 passed in 3.20s`。
- Task API：`7 passed in 3.22s`。
- M9 全部聚焦：`118 passed in 16.85s`。
- 全部 contract：`87 passed in 1.49s`。
- Backend 全量：`848 passed in 125.93s`，0 failed、0 skipped。
- Mock Runtime：`11 passed in 1.13s`。
- Backend `pip check`：`No broken requirements found.`；`compileall`：exit 0。
- Alembic：唯一 head/current 为 `0009_timeline_query_indexes (head)`；check clean；`0009 → 0008 → 0009` 成功且最终回到 0009。
- `check-scope.ps1 -Milestone M9`：`SCOPE_OK M9`。
- `check-sem-integrity.ps1`：`SEM_INTEGRITY_OK`，57 files，`total_size_bytes=2043071133`，fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。

## 已知风险

- Timeline cursor 签名密钥目前由本地环境配置提供；缺失时该单一路由按设计返回 503。部署或换机时必须用至少 32 UTF-8 bytes、无首尾空白和控制字符的 Secret 配置，不能提交真实值。
- Timeline 是跨多表的稳定读取投影，但并不冻结两次 HTTP 请求之间的业务状态；单次请求由只读可重复读快照保证一致，后续请求可合法看到 retry 或 Explanation 新事实。
- PostgreSQL 与 MinIO 之间既有非分布式事务边界仍然存在；M9 只读取已经持久化且通过来源一致性校验的 AVAILABLE Asset，没有扩大或掩盖该风险。

## M10-A 当前执行记录

- 已按实时 Git 修正 M9 为 `COMPLETE / PROJECT_OWNER_ACCEPTED`，验收提交为 `d7dee06c1f1294b010f64f5c532f5cb276ca53fa`。
- M10-A 开始基线为 `main@d7dee06c1f1294b010f64f5c532f5cb276ca53fa`；开始时工作区与暂存区为空。
- 已在阶段 1 计划中确认 M10-A/M10-B 拆分，并在 `check-scope.ps1` 中加入仅覆盖本工作单元的 M10 精确 allowlist；管理文件内部 gate 为 `SCOPE_OK M10`、`SEM_INTEGRITY_OK`、两个 diff check 均通过。
- Node/npm 为 `v24.14.0` / `11.9.0`。直接依赖全部精确锁定：Vue `3.5.40`、Vite `8.1.5`、`@vitejs/plugin-vue` `6.0.8`、TypeScript `6.0.3`、`vue-tsc` `3.3.8`、Vitest `4.1.10`、Vue Test Utils `2.4.11`、jsdom `29.1.1`、`@types/node` `26.1.1`。
- `npm --prefix frontend ci` 成功，安装 166 packages，audit 为 0 vulnerabilities；存在 dev-only 传递依赖弃用提示：`@vue/test-utils@2.4.11 → js-beautify@1.15.4 → glob@10.5.0`。
- TDD 红测分别确认 API Client、幂等状态机、轮询协调器和应用级 composable 模块尚不存在；实现后聚焦结果依次为 `24 passed`、`21 passed`、`14 passed`、`30 passed`。最终 `npm --prefix frontend run test -- --run` 为 4 files、`89 passed`、0 failed。
- `npm --prefix frontend run typecheck` 为 0 errors；`npm --prefix frontend run build` 使用 Vite `8.1.5` 成功构建 17 modules，生成的 `dist` 仅为被忽略的本地产物。
- 实现了原生 fetch Client、公共响应/错误类型、安全错误投影、写请求 Idempotency-Key、单个 sessionStorage 待定写操作、UNCERTAIN 恢复与同 key/body 手动重试、Timeline `limit=50` 全分页原序原子替换、AbortController + generation 陈旧响应保护、递归 timeout 单轮询协调器和应用级 `useMaterialsAgent`。
- 最小浏览器验收已通过：Vite 在 `127.0.0.1:4173` 启动，页面显示“材料智能体”“M10-A 前端基础已就绪”“前端数据层已加载”，API base 为 `/api/v1`，mutation 状态为 `IDLE`，控制台 0 warning/error；未触发真实 API 写请求，验收后已停止 dev server。未对 Backend proxy 进行可选的在线只读验证。
- 最终 `check-scope.ps1 -Milestone M10` 为 `SCOPE_OK M10`；SEM 为 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；`git diff --check` 与 `git diff --cached --check` 均为 exit 0。
- production source 与 `frontend/dist` 对 `object_key`、bucket/MinIO、Runtime token、Timeline signing key、PostgreSQL URL、weight path、用户绝对路径和 `SEM/` 的有界扫描均无命中；无 Backend、五份设计基线、SEM 或 M10-B component diff。
- 修改文件仅为三个管理文件与 `frontend/` M10-A allowlist 文件：前端环境/包与 TypeScript/Vite 配置、最小 App/CSS、API types/errors/client、三个 composable、测试 setup 和四个测试文件。`node_modules`、`dist`、coverage、其他 lockfile 均未进入 Git 状态。
- 已知风险：M10-A 仍是最小占位页而非完整产品 UI；手写 TypeScript 类型需要在 Backend 公共契约变化时同步；sessionStorage 的待定恢复只覆盖同一标签页；上述 dev-only `glob@10.5.0` 弃用提示等待上游依赖链更新。
- 当前只执行 M10-A：前端基础与可靠数据层；M10-B 和 M11 均未开始。
- 未执行 `git add`、commit、push、amend、rebase、reset、stash、分支或 worktree 操作。

## M10-A 首轮代码审查修订证据

- 首轮结论为 `M10_A_CODE_REVIEW: CHANGES_REQUESTED`；本轮仅修改审查允许的六个生产文件、四个测试文件和本动态进度文件，没有创建新生产文件、修改依赖或进入 M10-B。
- Critical 1 根因是 POST 与写后 GET reconciliation 位于同一 `try/catch`，导致已确认写入被后续读取失败重新报告为写失败。现已把服务器确认与 best-effort reconciliation 分成两个阶段：POST 成功立即固定 `SUCCEEDED`、清 pending、保存 POST request_id、执行 cache invalidation/补参目标清理；后续 GET 失败只显示固定安全刷新提示且不重新抛出。
- Critical 2 根因是切换 Conversation ID 时沿用旧 Timeline，目标 Conversation 首次 GET 失败后仍会展示旧 Conversation 数据。现已在真实 ID 切换时立即清空 Timeline；同一 Conversation 的普通 refresh 失败仍保留现有完整 Timeline。
- 红测：API Client 为 `6 failed, 29 passed`，失败覆盖 GET/Conversation 网络分类和 Asset URL 白名单；幂等状态机为 `7 failed, 21 passed`，覆盖四类 body 校验、255 字符边界和 Unicode control；轮询为 `1 failed, 14 passed`，实际错误为 3 次 poll 而预期 2 次；应用级 composable 为 `13 failed, 30 passed`，覆盖 POST/GET 分离、Conversation 混用、Task history 竞态、UNCERTAIN 保留和 scope dispose；`warnings: unknown[]` 类型红测产生 2 个 `TS2344`。
- Task history 现按 `task_id` 使用 AbortController + generation；retry invalidation 会 abort/失效旧 GET，较早并发请求不再提前清除较新请求的 loading 状态。
- API Client 现区分读取网络失败、四类幂等写结果不确定和 Conversation 创建结果不确定；Conversation 创建固定提示先刷新列表避免重复创建，不自动重试。
- sessionStorage descriptor 已改为 operation-specific 判别联合并按 operation 严格校验 body；损坏记录安全清除且不发送。Idempotency-Key 允许最大 255 字符，并拒绝 Unicode control character。
- Asset 公共 URL 只接受 `/api/v1/assets/{asset_id}/content` 及其 query；拒绝第三方绝对 URL、protocol-relative、javascript/data 和其他公共路径。绝对 API base 只提供解析 origin，attachment 保留既有 query。
- 有效 Vue scope dispose 会停止 polling、移除 visibility listener，并 abort Conversation、Timeline 和全部 Task history GET；generation 同时阻止不响应 abort 的迟到响应写回状态。
- hidden 时会清除 queued poll trigger，恢复 visible 后只执行一次立即 poll。成功后台 GET 不再清除仍需用户处理的 `UNCERTAIN` 提示。
- Vue 在 `package.json`、`package-lock.json` 和 `node_modules` 三处均为 `3.5.40`；直接依赖版本未修改。`npm ci` 成功，audit 为 0 vulnerabilities，保留既有 dev-only `glob@10.5.0` 上游弃用提示。
- 修复后聚焦结果为 API Client `36 passed`、幂等状态机 `28 passed`、轮询 `15 passed`、应用级 composable `43 passed`；全量为 4 files、`122 passed`、0 failed。
- 最终 `npm --prefix frontend run typecheck` 为 0 errors；Vite `8.1.5` build 成功、17 modules；`SCOPE_OK M10`；SEM 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；两个 diff check 均为 exit 0。
- 未发现 `as unknown as`、生产 `any` 或 skipped tests；暂存区为空，未执行 commit、push 或 amend。

## M10-A 最终代码复审修订证据

- 最终复审结论为 `M10_A_FINAL_CODE_REVIEW: CHANGES_REQUESTED`；本轮只处理最后两个 Important 和一个 Minor，没有修改依赖、lockfile、计划、scope、Backend、CORS、设计基线、`SEM/`、Mock Runtime 或 M10-B 文件。
- 开始前已删除仓库根目录未跟踪审查传输文件 `m10-a-code-review-revision.zip`；随后及最终检查的根目录 `*code-review*.zip` 数量均为 0，Git 状态也没有 zip、`node_modules`、`dist` 或 coverage。
- 定向红测为 2 files、`8 failed / 81 passed`：Conversation 创建非 JSON 的 HTTP 200/500/503 共 3 项；discard 后旧提示 1 项；Conversation 切换期间 dispose 后 polling 复活 1 项；普通幂等写、pending retry 和 Conversation 创建在 POST 晚于 dispose 成功后仍触发 GET 共 3 项。
- `loadConversations`、Timeline 两个读取入口、`selectConversation`、Task history 和写后 reconciliation 现有明确 disposed 生命周期门；`selectConversation` 只在 `restartPolling && !disposed` 时恢复 polling，公开 `startPolling` 也不会在已销毁 scope 上启动。切换 B 的迟到响应不写 Timeline，推进 fake timers 60 秒没有新增 Timeline GET，visibility listener 的 add/remove 数量配平。
- 已发送的写 POST 没有取消或自动重试。服务器明确成功后，幂等状态机仍进入 `SUCCEEDED` 并清除 pending；若 scope 已 dispose，普通写和 pending retry 都不执行 cache invalidation、页面状态写回或 Timeline/Conversation reconciliation。Conversation 创建晚到成功同样直接返回已创建对象且不启动 GET。
- API Client 在 Conversation 创建响应无法解析 JSON 时，无论 HTTP 200、500 或 503，均抛出固定安全的 `ConversationCreationUncertaintyError`，提示先刷新列表以避免重复创建；普通 GET 和幂等写的非 JSON 响应仍是 `ProtocolResponseError`，不暴露原始 HTML 或响应正文。
- `discardPendingMutation()` 只有实际丢弃 pending descriptor 时才清除旧 `globalError`；成功丢弃后状态为 `IDLE`、pending 为 `null`、globalError 为 `null`，没有 pending 时保留无关错误。
- 本轮新增 10 项测试；定向绿测为 2 files、`89 passed`，全量为 4 files、`132 passed`、0 failed。`npm ci` 安装 166 packages、audit 0 vulnerabilities；保留既有 dev-only `glob@10.5.0` 上游弃用提示。typecheck 为 0 errors，Vite `8.1.5` build 成功、17 modules。
- `SCOPE_OK M10`；`SEM_INTEGRITY_OK` 为 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；两个 diff check 均为 exit 0。Vue 在 `package.json`、lockfile 和实际安装三处均为 `3.5.40`。
- 当前仍为 `main@d7dee06c1f1294b010f64f5c532f5cb276ca53fa`，暂存区为空；未执行 `git add`、commit、push 或 amend，M10-B 和 M11 均未开始。

## M10-A 项目负责人验收

- 项目负责人已验收 M10-A：`COMPLETE / PROJECT_OWNER_ACCEPTED`。
- M9 保持 `COMPLETE / PROJECT_OWNER_ACCEPTED`，acceptance commit 为 `d7dee06c1f1294b010f64f5c532f5cb276ca53fa`。
- M10-A acceptance commit 已创建，为 `c597a89340072217d07d734e6b2ab88b6236d009`。
- 暂存区精确包含 M10-A 的 28 个批准路径；没有 scope 外暂存路径。
- M10-B 与 M11 均为 `NOT STARTED`；未执行 push 或 amend。

## M10-B 当前执行记录

- M10-B 开始基线为 `main@c597a89340072217d07d734e6b2ab88b6236d009`（`feat: add reliable frontend foundation`），parent 为 `d7dee06c1f1294b010f64f5c532f5cb276ca53fa`。
- M10-A 为 `COMPLETE / PROJECT_OWNER_ACCEPTED`，acceptance commit 为 `c597a89340072217d07d734e6b2ab88b6236d009`。
- 开始时工作区与暂存区为空；Node/npm 为 `v24.14.0` / `11.9.0`，Vue 为 `3.5.40`。
- 已完成完整最小 Vue 界面：对话侧栏、Conversation/Timeline、三类顶层条目、Tool Task 状态卡、结构化 Result、Asset 预览与下载、ToolRun 历史、补充输入、Tool/Explanation 重试、全局安全错误和 `UNCERTAIN` 恢复入口；组件只消费 M10-A composable 与公共 `/api/v1` 投影。
- TDD 红测分别证明 Timeline、Conversation Sidebar、Composer、完整 Tool 卡与 App 流程尚未满足；绿测为 5 个组件测试文件、`46 passed`。与 M10-A 既有 132 项合并后，全量为 9 files、`178 passed`、0 failed。
- `npm --prefix frontend ci` 安装 166 packages，audit 为 0 vulnerabilities；保留既有 dev-only `glob@10.5.0` 上游弃用提示。`npm --prefix frontend run typecheck` 为 0 errors；Vite `8.1.5` production build 成功，39 modules，JS bundle 110.36 kB（gzip 38.67 kB）。
- 浏览器 A–H 验收均通过：新建与切换多个 Conversation 不混用 Timeline；知识问答正确；`NEEDS_INPUT → 补充 → SUCCEEDED`；Result、Explanation、图片预览和真实下载事件可用；Mock Runtime 暂停后第 1 次 ToolRun `FAILED`，恢复后 UI 重试为第 2 次 `SUCCEEDED` 且历史保留；安全 Mock Explanation failure 产生 `PARTIALLY_SUCCEEDED`，恢复默认 adapter 后 UI 重试为 `SUCCEEDED`；MinIO 暂停时仅图片区显示“图片加载失败。结构化结果仍可查看”，恢复后图片重新加载；Backend 暂停时出现 `UNCERTAIN`，页面正文不含幂等标签或 UUID，恢复后“使用原请求重试”成功。
- 浏览器最终 console warning/error 为 0；页面无横向溢出。Backend 访问日志中的浏览器业务请求均为公共 `/api/v1` Conversation、Timeline、Task retry、Explanation retry 与 Asset content 路径；前端源码和 production bundle 的敏感词/内部路径扫描无命中。
- `check-scope.ps1 -Milestone M10` 为 `SCOPE_OK M10`；`SEM_INTEGRITY_OK` 为 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；`git diff --check` 与 `git diff --cached --check` 均为 exit 0。
- 五份确认设计基线、阶段 1 计划、Backend、CORS、数据库模型/migration、Mock Runtime、M10-A 数据层与 `SEM/` 均未修改；未加载或运行真实模型。
- 未执行 `git add`、commit、push、amend、rebase、reset、stash、分支或 worktree 操作；M11 未开始。

## M10-B 第一轮代码审查修订证据

- 第一轮审查结论为 `M10_B_CODE_REVIEW: CHANGES_REQUESTED`；本轮只修改审查明确允许的既有页面、composable、组件、测试与本动态进度文件，没有创建新生产组件或测试文件。
- `useMaterialsAgent` 已把用户明确写操作错误与后台读取错误拆成 `actionError` / `readError`，公开 `globalError` 固定投影 `actionError ?? readError`。Conversation list、Timeline 和 Task history 只清理/设置读取错误；HTTP 409、422、500、503、504、写操作不确定和写后 reconciliation 安全提示均不会被后台读取清除或覆盖。
- Conversation 创建非 JSON/网络结果不确定时设置 `conversationCreationUncertain=true` 并保留安全 action error；此时再次创建被 composable 与 UI 双重阻止。新增 `refreshConversations()` 只执行 Conversation 首页 GET，成功后解除不确定并只清除对应创建提示，失败时保留不确定与创建提示；没有自动重试创建或生成 Idempotency-Key。
- Sidebar 新增“刷新对话列表”只读入口；列表 loading 时禁用，创建不确定时仍可用，不改变 Backend 顺序。创建不确定解除前只禁用“新建对话”，已有幂等保护的其他写操作保持既有策略。
- `ConversationView` 以 `selectedConversation.conversation_id` 作为 `ChatComposer` key。Conversation A 的普通或补参草稿切到 B 后随旧组件销毁；同一 Conversation 内取消补充不改变 key，草稿继续保留。
- App 的 `writeBusy` 同时覆盖 Conversation create POST、幂等 mutation `SENDING` 与 `UNCERTAIN`；它统一禁用 Composer、补充、Tool retry、Explanation retry 和新建 Conversation，但不禁用 Conversation 选择、Timeline 刷新、Conversation 列表刷新或 Task history。
- 单张图片失败后可点击“重新加载图片”：只清理该 Asset 的失败状态、恢复 loading 并递增 per-asset render generation，使同一公共 URL 的 `<img>` 重新挂载；不增加 query、不 fetch、不构造 Blob，非法 URL 没有重试入口，Result 与 Explanation 保持可见。
- NEEDS_INPUT 的 `normalized_input` 现以安全 definition list 展示普通嵌套对象和标量数组；工艺参数的 `1000`、`°C`、`3`、`h` 可见。展开最大深度为 3，超深内容显示“嵌套内容未展开”，函数、特殊对象、原型内容和 getter 异常不展开；不使用 `v-html` 或 JSON dump。
- 必修行为 RED 为 4 files、`13 failed / 79 passed`，每个失败均命中审查指出的缺失行为；逐项 GREEN 后为 4 files、`95 passed`。过程中 typecheck 暴露 1 个测试 fixture 的可迭代类型错误，修正 fixture 后为 0 errors。
- `npm ci` 安装 166 packages、audit 0 vulnerabilities，保留既有 dev-only `glob@10.5.0` 上游弃用提示。最终前端全量为 9 files、`193 passed`、0 failed；typecheck 0 errors；Vite `8.1.5` build 成功、39 modules，JS 112.45 kB（gzip 39.15 kB）。
- production source/bundle 的 Secret、数据库/Object Storage/Runtime 内部值、绝对路径与 `/internal/v1` 扫描无命中；组件无直接 fetch、Idempotency-Key 生成、`v-html`、`setInterval`、console 输出或显式 `any`。
- `SCOPE_OK M10`；`SEM_INTEGRITY_OK` 为 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；tracked、cached 和 16 个未跟踪文件 whitespace check 均通过。
- 本轮未允许修改的 7 个既有 M10-B dirty 路径经 SHA-256 前后对比完全不变。Backend、CORS、数据库/migration、五份设计基线、`SEM/`、Mock Runtime、依赖/lockfile、Vite 配置和 M11 均未修改。
- 暂存区为空；未执行 `git add`、commit、push、amend、rebase、reset、stash、分支或 worktree 操作。

## M10-B 最终代码复审错误恢复修订证据

- 最终复审结论为 `M10_B_FINAL_CODE_REVIEW: CHANGES_REQUESTED`；本轮只修改 `App.vue`、`useMaterialsAgent.ts`、`GlobalErrorNotice.vue`、两份指定测试和本动态进度文件，没有创建文件，也没有修改 Backend、CORS、数据库/migration、设计基线、`SEM/`、Mock Runtime、依赖/lockfile、Vite 配置或 M11。
- `runMutation()` 与 `retryPendingMutation()` 只在捕获 `ApiResponseError` 后进入明确业务失败恢复：先保存原 `MUTATION` action error，再 best-effort 读取当前 Conversation 的完整 Timeline；`TASK_CREATE` / `TASK_INPUT_SUPPLEMENT` 还读取 Conversation 首页。GET 失败只写安全 read error；原 `ApiResponseError` 最后原样抛出。没有重发 POST、生成新 key、改变 `BUSINESS_FAILED` 或保留已按既有逻辑清除的 pending descriptor。网络 `UNCERTAIN` 不触发任何权威 GET。
- action/read 通知现由独立槽位投影为 `globalErrors`，顺序为 Conversation 创建不确定、action、read，并按 `message/status/request_id` 去重；兼容的 `globalError` 保留。`GlobalErrorNotice` 可同时渲染安全业务错误与读取错误，不显示 raw body、stack、内部路径或幂等 key；pending mutation 恢复区保持原样。
- Timeline 成功读取只在 `actionErrorSource === RECONCILIATION` 时清除写后刷新警告；手动刷新和既有 polling 共用该成功路径。失败保留警告；`MUTATION` 业务错误、Conversation 创建不确定、pending `UNCERTAIN` 均不会被清除。
- Conversation 创建不确定由 `conversationCreationUncertain` 独立投影固定安全通知，不再占用 action 槽；后续 409、422 或 reconciliation error 不能隐藏它。只有显式 `refreshConversations()` 成功后才解除；失败时可与 Conversation 读取错误并存，新建按钮继续禁用且不自动重试创建。
- TDD RED 为两文件 `15 failed / 67 passed`：5 个参数化 HTTP 409/422/500/503/504 用例均证明明确错误后 Timeline GET 缺失，其余失败分别命中多通知 API/UI、reconciliation 成功恢复、创建不确定独立性和安全过滤缺口。最小实现后同组为 `82 passed`；网络 `UNCERTAIN` 不刷新的保护用例在红、绿两阶段均通过。
- `npm --prefix frontend ci` 安装 166 packages；npm registry 当前审计元数据报告 6 high severity vulnerabilities，并保留 `glob@10.5.0` 弃用提示。本轮禁止修改依赖版本与 lockfile，未执行会改依赖的 audit fix。最终 typecheck 为 0 errors；前端全量为 9 files、`204 passed`、0 failed；Vite `8.1.5` build 成功、39 modules，JS 113.20 kB（gzip 39.43 kB）。
- production source/bundle 对 object key、MinIO、Runtime token、Timeline signing、数据库 URL、`/internal/v1`、模型路径、`SEM/`、用户绝对路径和内嵌凭据的 10 组有界扫描为 `PRODUCTION_SECURITY_SCAN_OK`；允许修改的生产/测试文件没有新增显式 `any`、`as unknown as`、`v-html`、`setInterval`、console 输出，组件没有直接 fetch。
- `SCOPE_OK M10`；`SEM_INTEGRITY_OK` 为 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。本轮禁止修改的 16 个既有 dirty 文件经 SHA-256 前后核对完全不变。
- `git diff --check` 与 `git diff --cached --check` 均为 exit 0；本轮修改的两个未跟踪文件也通过独立 whitespace check。最终暂存区为空，Git dirty 文件仅为既有 M10-B 前端/测试、scope 脚本和当前进度文件；没有 zip、`node_modules`、`dist`、coverage、Backend、设计基线、`SEM/` 或 M11 dirty 路径。
- 该修订轮未执行 `git add`、commit、push、amend、rebase、reset、stash、分支或 worktree 操作；当前项目负责人验收与提交授权见下节，M11 未开始。

## M10-B 项目负责人验收与提交授权

- 项目负责人已验收 M10-B：`COMPLETE / PROJECT_OWNER_ACCEPTED`；M10 overall 同步为 `COMPLETE / PROJECT_OWNER_ACCEPTED`。
- M10-B 与 M10 overall 的 acceptance commit 均已创建，为 `f426e6f23703002a648991e5bb436929df19e8e2`；subject 为 `feat: add minimal chat frontend`。
- 验收证据继续保留：前端全量 9 files、`204 passed`、0 failed；typecheck 0 errors；Vite `8.1.5` production build 成功、39 modules，JS 113.20 kB（gzip 39.43 kB）。
- 浏览器 A–H 验收继续有效：Conversation、知识问答、Tool、补参、两类重试、图片查看/下载、安全错误、MinIO/Backend 故障与 `UNCERTAIN` 原 key 恢复均已覆盖，最终 console warning/error 为 0。
- SEM 证据继续有效：`SEM_INTEGRITY_OK`，57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- `npm ci` 的当前审计元数据报告 6 个 high severity vulnerabilities；它们仅来自开发依赖。提交前只读复核 `npm --prefix frontend audit --omit=dev` 为 `found 0 vulnerabilities`。既有 `glob@10.5.0` 弃用提示同样来自 dev-only 传递依赖；本轮未修改依赖或 lockfile。
- M10 验收提交后的 M11-A 实现见下节；M10-B 提交后未 push、未 amend。

## M11-A 当前执行记录

- 唯一开始基线已实时核对为 `main@f426e6f23703002a648991e5bb436929df19e8e2`，subject `feat: add minimal chat frontend`，parent `c597a89340072217d07d734e6b2ab88b6236d009`；开始时工作区和暂存区均为空，未创建分支、worktree 或临时 commit。
- 阶段 1 计划中的 M11 已拆为 M11-A 与 M11-B；`check-scope.ps1` 已分别配置 `M11A`、`M11B` 和二者并集 `M11`。本次限定修订确认 M11-B allowlist 已预配置，开始 M11-B 时只需重新核对而不是扩展；`check-scope.ps1` 内容与修订前 SHA-256 一致。本轮仍只实现 M11-A 的七个精确 allowlist 路径，没有创建 M11-B 验收矩阵、统一入口或报告。
- `start-mock-stack.ps1` 现在把 `APP_ENV`、`LOG_LEVEL`、Actor、PostgreSQL、MinIO、Runtime、M5 开关和 Timeline key 共 20 个字段纳入同一受控环境；`APP_ENV=local`、`LOG_LEVEL=INFO`、PostgreSQL 端口、Runtime timeout 和 `M5_DEV_ROUTES_ENABLED=false` 使用与 `.env.example`/`AppSettings` 一致的安全默认，其余资源身份或凭据字段必须由 `.env` 提供。完整字段先经现有 `load_settings()`、PostgreSQL URL、MinIO 和 Runtime 解析校验，再覆盖 Compose、Alembic、bucket bootstrap、失败清理和三个子进程创建；所有 return/catch 路径最后逐项恢复调用者环境。
- PowerShell 环境备份/恢复继续使用进程级 .NET API；三个隐藏 WMI wrapper 改为通过 `New-RoleSpecificChildEnvironment` 接收 role-specific 环境，而不是完整受控环境。函数探针确认 Backend 含全部 20 个已验证配置键；Runtime 的 M11-A 业务键精确为 `ZTA35G_RUNTIME_TOKEN` 和固定 `ZTA35G_RUNTIME_PORT`，不含 PostgreSQL、MinIO、Actor、Timeline 或 M5 字段；Frontend 不含任何受控字段，因此五个 Backend Secret 均缺席。通用 PATH、SystemRoot、TEMP、USERPROFILE 和既有 Conda 运行环境仍保留；实际值不进入命令行、state、日志或测试输出。
- 所有 Compose `ps/up/stop` 固定 `materialsagent` project 和仓库 `docker-compose.yml`，并在首次 Compose 查询前解析实际有效 Docker context/endpoint/engine ID。只接受 `npipe:////./pipe/<local-name>`；`DOCKER_HOST=tcp://...` 的完整 start 探针在 `docker_validation` 阶段、任何 Compose 副作用前以不含 endpoint 详情的固定错误拒绝。正常 state 和 recovery state 均记录非空 `docker_engine_id/docker_context`。真实篡改 engine ID 后，stop 仍安全停止三种 App 进程，但输出 `DOCKER_OWNERSHIP_NOT_VERIFIED`、保留 state、两个 Compose 服务继续运行；恢复可信 identity 后才停止本轮服务并删除 state。
- 本轮再次在两个服务均非 preexisting 时真实执行 `compose up`，随后注入 ownership reconciliation 查询失败并让首次 cleanup 受控失败；输出 `MOCK_STACK_START_CLEANUP_INCOMPLETE`，recovery state 为 `process=[]`、两个合法 Docker record 和同一 engine/context、无 Secret。独立 stop 随后只停止本轮 `postgresql/minio` 并删除 state；未执行 `down`、`down -v` 或删除 volume。
- start/stop 的 state 结构校验现在同时支持脚本内 `[ordered]` state 与 JSON `PSCustomObject`，但显式要求非空 `run_id/created_at/python_executable` 以及存在、非 null、集合语义明确的 `process/docker`。函数矩阵确认空数组 recovery 合法，null、缺失集合、字符串、普通对象和缺失 metadata 均非法；真实 `process=null`、`docker=null`、缺失 process、缺失 docker 四项 stop 均 exit 2，损坏 state 原样保留、三个可信 PID 和两个 Compose 服务保持运行，恢复可信 state 后才正常停止。
- 三个应用进程仍使用固定 role/marker/port、Windows `SystemDirectory\cmd.exe`、PID/start time、实际命令行 repo root 交叉验证。WMI 返回 PID 后现在立即建立 provisional record 并登记到外层 ownership list，再进行完整 CIM metadata 验证。真实故障探针让 metadata 失败、首次精确 PID rollback 和外层第二次 rollback 均受控失败；recovery state 保留一条完整 runtime provisional record、Docker 已清理，随后独立 stop 在实际 metadata 可验证后终止该精确 PID 并删除 state。未知/重复 role/service、marker、wrapper 或 ownership 仍 fail-closed。
- 两轮最终启停均成功：第一轮 start 后正常 state 为 3 process/2 Docker 且 local engine identity 与实时值一致，重复 start 返回 `MOCK_STACK_ALREADY_RUNNING` 且 run_id/PID 集合不变；identity mismatch 探针恢复可信 state 后正常 stop，再次 stop 返回 `MOCK_STACK_NOT_RUNNING`。第二轮 start 后四项 null/missing state 均拒绝且 3 process/2 Docker 不变，恢复可信 state 后 E2E 与正常 stop 成功。额外全量验证栈也正常 start/stop；最终 3000/8000/8100 listener、state 和 Compose running service 均为 0，未删除 volume。
- TDD RED：三条 E2E 均被收集，fixture 实现前精确为 `3 errors`，原因均为 `fixture 'e2e_harness' not found`；没有语法、导入或生产代码失败。
- GREEN E2E 使用真实临时 PostgreSQL 数据库、真实临时 MinIO bucket、真实 `127.0.0.1:8100` Mock Runtime HTTP、进程内 Mock Chat/Explanation 和公共 API。fixture 以同一个受控 MinIO client 先检查同名 bucket 不存在，再创建并设置 `bucket_created=true`；只有本轮成功创建才允许清理，清理函数再次验证 bucket 名。新增 fake-client 回归证明预存同名 bucket 立即拒绝且 sentinel object/bucket 不被创建、枚举或删除；两轮 E2E 均为 `4 passed`（`1.78s` / `1.64s`）。
- 完整 Tool 旅程的统一 PNG 安全断言继续验证精确 `image/png`、受控 filename、PNG magic bytes 和所有响应头；本轮还直接读取真实 `AssetRow.object_key` 并确认其实际值未出现在任何 header。测试未暴露生产 Asset API 缺陷，未修改生产代码。
- E2E session cleanup 后只读审计为 `E2E_DATABASE_LEFTOVERS=0`、`E2E_BUCKET_LEFTOVERS=0`；正式数据库和正式 bucket 均仍存在。最终 `state.json` 不存在，3000/8000/8100 listener 均为 0，Compose running service 为 0；日志保留。
- 本轮 Backend E2E 为 `4 passed in 1.86s`；直接使用既有 Backend Python 的最终全量为 `852 passed in 112.16s`。Mock Runtime 为 `11 passed in 1.08s`；`pip check` 为 `No broken requirements found.`，`compileall` exit 0。
- Frontend Vitest 为 9 files、`204 passed`；typecheck 0 errors；Vite `8.1.5` production build成功、39 modules，JS 113.20 kB（gzip 39.43 kB）。
- Alembic 唯一 head/current 为 `0009_timeline_query_indexes (head)`，`upgrade head` 成功，`alembic check` 为 `No new upgrade operations detected.`。
- `SCOPE_OK M11A` 与 `SCOPE_OK M11`；`SEM_INTEGRITY_OK` 为 57 files、`total_size_bytes=2043071133`、fingerprint `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`；未修改、加载或运行 `SEM/`。
- 根 `.env` 在远程 endpoint、engine mismatch、provisional、Compose recovery、null/missing state、两轮启停和全量验证后均恢复到修订前 SHA-256 `EEA078F8072D4D3C57B4AA046736B4B15E09B182675C33C9A5234A06C79C4EAB`；受控 Timeline 测试 key 未保留。最终只读审计为 `E2E_DATABASE_LEFTOVERS=0`、`E2E_BUCKET_LEFTOVERS=0`，正式数据库和正式 bucket 均仍存在；没有调用真实 LLM、真实 `materialsagent-zta35g`、GPU 环境或模型权重。
- 当前未提交路径精确为：本计划、当前进度文件、`scripts/dev/check-scope.ps1`、`scripts/dev/start-mock-stack.ps1`、`scripts/dev/stop-mock-stack.ps1`、`backend/tests/e2e/conftest.py`、`backend/tests/e2e/test_mock_journey.py`。本轮已授权精确暂存这七个 M11-A allowlist 路径；未执行 commit、push 或 amend。
- M11-A code review: `APPROVED`。M11-A project-owner manual acceptance: `PASSED`，manual acceptance date: `2026-07-27`。稳定人工验收摘要：`start-mock-stack.ps1` 输出 `MOCK_STACK_STARTED`；Frontend 页面正常访问；Conversation 创建正常；知识问答正常；刷新后 Conversation 和 Timeline 可恢复；Frontend 代理 Backend health 正常；`stop-mock-stack.ps1` 输出 `MOCK_STACK_STOPPED`；三个受控应用进程均由 stop 脚本精确停止。

## M11-B 当前执行记录

- M11-B start baseline 为
  `main@4ed740222238433541fb31c993dd75610634d157`，主题
  `feat: add reliable mock stack acceptance`，父提交为
  `f426e6f23703002a648991e5bb436929df19e8e2`；开始前工作区和暂存区均为空。
- 已完整读取 M11-B 指令、AGENTS、当前进度、阶段 1 计划对应范围、直接相关
  生产调用链和回归测试。生产修复严格限制为 storage port、MinIO adapter 与
  AssetService；migration、依赖、设计基线、ToolWorkflow/API、M11-A journey
  和 start/stop 均未修改。
- M11-B1 TDD RED 先落四类代表测试，精确为 `4 errors`，唯一原因是
  `fixture 'e2e_app_factory' not found`；不是语法、导入或生产代码失败。实现
  test-only factory/HTTP harness 后，首次 GREEN 暴露测试错误地期待失败响应含
  `data`，实际安全 502 使用 `resource.task_id`；只修正测试断言，未改生产代码。
- `conftest.py` 现提供共享真实临时 PostgreSQL/MinIO 的 `E2EAppFactory`，
  以及动态 loopback 端口、随机 test token、后台 uvicorn thread、启动探针、
  `execution_count`、有界 event、join 和端口重新 bind 的 test-only Runtime HTTP
  harness。支持 success、partial success、fail-once、timeout、busy 和 unavailable；
  test token 不写日志。
- M11-B fault matrix 精确收集 20 cases：success 1、input error 5、dependency
  failure 7、idempotency 5、retry 2、asset/security 2。修正 MinIO 断言前的
  历史 M11-A + M11-B 聚焦 E2E 为 `24 passed`；JUnit 名称统计由统一入口
  读取，不解析 pytest 人类文本。
- Tool retry E2E 证明同一 Task 保留失败 attempt 1、新增成功 attempt 2、
  seed 不同、selected Run/Result 指向 attempt 2、唯一 Result/Asset 属于新
  Run、Timeline item/anchor 稳定；Runtime 总调用 2 次，同 key replay 不产生第
  3 次调用或资源增量。
- Explanation retry E2E 证明同一 Result 下保留 FAILED/SUCCEEDED 两次
  Explanation 和两条 LLMCall；Runtime、ToolRun、Result、Asset、link、MinIO
  object 和 Timeline anchor 不变；同 key replay 零增量。
- `run-phase-1a.ps1` 可从任意当前目录定位仓库根，只调用现有 start/stop；
  每次写独立 `tmp/phase-1a-acceptance/<run-id>/`，`commands.json` 仅含命令名、
  起止时间、退出码和相对日志路径。Runner 现于 start 前验证 branch=`main`、
  staging empty 及 M11-A acceptance commit 为 HEAD 祖先，并在 summary
  记录当前 branch/HEAD/subject/parent；错误分支、detached HEAD 或基线缺失
  均非零退出、仍写 summary 且不启动 Mock Stack。
- Runner 将 `python --version`、`node --version`、`npm --version`、
  `docker compose version`、`docker compose config --services` 和
  `docker compose config --images` 分别记录为独立 command；安全解析结果进入
  summary。受控证据探针为 Python `3.11.15`、Node `24.14.0`、npm `11.9.0`、
  Compose `5.3.0`、services 精确为 `minio, postgresql`，images 与当前 Compose
  固定 digest 完全一致。
- Runner 安全扫描已与扩展后的精确 17 路径 M11-B allowlist 对齐；原 15
  路径继续保留，并新增默认 Mock responder 生产文件和 Chat orchestration
  contract test。覆盖探针为
  `ACCEPTANCE_SCAN_COVERAGE_OK count=17`，并在启动 Mock Stack 前退出。
  动态扫描继续覆盖 logs、`commands.json`、`summary.json` 和 JUnit XML。
- Runner 自身受控失败探针均通过：复用既有栈时 exit 1、
  `summary.json` 存在、`failed=1`、state 哈希和三个 listener 不变；无既有栈时
  `started_by_runner=true`，exit 1 后 state、3000/8000/8100 listener 和 Compose
  running service 均为 0。函数级 marker 探针为
  `MANUAL_MARKER_LOGIC_OK`；最终权威验收在项目负责人完成 11 项人工清单后
  记录 `browser_manual_scenarios=11`。
- MinIO 修复 TDD RED 精确为 78 项收集、`72 passed / 6 failed`；失败仅来自
  outcome-unknown 类型/映射缺失、pre-write Asset 实际 `PENDING` 而预期
  `FAILED`，以及失败事实 commit 未进入持久化分支，无语法、导入、fixture
  或路径错误。其后聚焦集合为 `78 passed`，Backend E2E 为 `24 passed`。
- 生产链现为：`AssetService._create_pending()` 独立短事务提交 PENDING；
  `MinioStorageService.put()` 在前置 HEAD 失败时抛普通
  `StorageUnavailableError`，在进入 `put_object()` 后的不可确认异常抛
  `StorageWriteOutcomeUnknownError`；AssetService 对前者提交 FAILED，对后者
  保留 PENDING。失败事实 update/commit 失败时回滚并传播持久化错误，不伪造
  FAILED。ToolWorkflow 仍只负责独立终结 Task/ToolRun。
- 阶段 1A 权威验收 run id 为 `20260728T075704Z-c0e323e1c2bf`：
  `PHASE_1A_ACCEPTANCE_PASSED`、`failed=0`、`browser_manual_scenarios=11`；
  success 1、input error 5、dependency failure 7、idempotency 5、retry 2、
  asset/security 2。M11 E2E 为 `24 passed`、Backend 全量为 `882 passed`、
  Mock Runtime 为 `11 passed`、Frontend 为 9 files / `204 passed`；
  typecheck/build 为 PASS；Alembic head/current 均为
  `0009_timeline_query_indexes`，check 为 PASS。
- 正式静态报告位于 `docs/acceptance/phase-1a-report.md`，状态已统一为
  `PHASE_1A_PROJECT_OWNER_ACCEPTED_STAGING_AUTHORIZED`；M11-B 已验收但尚无
  acceptance commit hash。
- 实际最小生产 allowlist 为
  `backend/src/materialsagent/domain/ports/storage.py`、
  `backend/src/materialsagent/infrastructure/storage/minio.py` 和
  `backend/src/materialsagent/application/asset_service.py`；
  migration=`NO`，public API change=`NO`。
  三类写入/持久化结果已由回归测试分别锁定。
- 浏览器第 5 项首次使用真实补参 `730 °C` 时稳定返回 HTTP 502；临时魔法词
  验证成功，确认默认 Mock 把短补参误路由为 KnowledgeAnswer。缺陷已通过
  TDD 修复，contract 只接受受控时效温度表达；M11-A journey 和 M11-B
  supplement replay 均改用 `730 °C`。聚焦 RED 为 42 项中的
  `35 passed / 7 failed`，五种合同表达均误路由为 KnowledgeAnswer，journey
  与 replay 均得到安全 HTTP 502；最小实现后同组为 `42 passed`。
- 项目负责人于 2026-07-28 使用全新 `NEEDS_INPUT` Task 复测第 5 项，只输入
  `730 °C`；未出现 HTTP 502，同一 Task 最终 `SUCCEEDED`，结构化 Result 与
  Explanation 正常，Timeline 卡片位置不移动。Frontend、Conversation、知识
  问答、完整 Tool、图片预览/下载、刷新恢复和 Timeline 稳定均为 `PASSED`。
  Tool retry 与 Explanation retry 由 M10 人工验收和 M11-B E2E 覆盖；本轮正式
  数据库无可操作失败卡片，未人为制造故障。
- 稳定门禁证据为 `SCOPE_OK M11B`、`SCOPE_OK M11`、
  `SEM_INTEGRITY_OK`；SEM 为 57 files，fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- 本工作单元当前共 15 个 dirty 路径，全部属于扩展后的精确 17 路径 M11-B
  allowlist；其余已审查生产修复和测试保持冻结。

## M12-A 当前执行记录

- 开始门禁实时核对为
  `main@f5e24dcaab4801dbeffb8400f2960c33b60b4f00`，subject
  `feat: complete phase 1a mock acceptance`，parent
  `4ed740222238433541fb31c993dd75610634d157`；开始时仅
  `backend/pyproject.toml` 为已审查依赖 diff，暂存区和 untracked 均为空。
- 依赖恢复副本为 `materialsagent-backend-pre-m12a-20260728`；已核实
  `langchain-deepseek==1.1.0`、`langchain-core==1.4.9`、
  `langchain-openai==1.3.5`、`openai==2.46.0`、`tiktoken==0.13.0`、
  `httpx==0.28.1`，`pip check` 通过。本轮没有继续修改依赖元数据。
- 配置默认 `LLM_ADAPTER=mock`；DeepSeek 只接受固定 model、根地址与 timeout，
  Key 为 `SecretStr | None`，全空白归一化为 None，非空 Key 若含首尾空白则
  fail closed 且不静默 trim。DeepSeek wiring 缺 Key fail closed；未读取
  `.env`，未设置或请求真实 Key，真实 Provider calls=0。
- Chat/Explanation Port 已增加不可变 request metadata；Chat 返回 outcome。
  DeepSeek Chat 使用严格 Provider Schema 与 JSON mode，Explanation 使用独立
  model。两者固定 thinking disabled、SDK retries=0、streaming=false，
  request metadata 与 invoke 共享 Prompt 渲染；数据库只保存模板身份、SHA-256
  digest、五键 generation parameters、白名单 usage/request-id 和静态错误。
- Application 仍在事务外调用 Provider；Chat 成功保存 outcome usage/request-id，
  失败在 LLMCall 保存细分类静态错误，而公共 Task/API 保持既有 502/503/504
  映射。Explanation 初始调用与显式 retry 均使用 Adapter metadata，保留既有
  commit uncertainty 与幂等语义。
- `create_app` 保持显式注入优先；Mock 模式不构造 DeepSeek model，DeepSeek
  模式构造 Chat/Explanation 两个不同实例并共享冻结配置，无静默 fallback。
- 离线合同已覆盖三条 Chat route、Explanation、严格 Schema、usage/request-id
  白名单、completion id 排除、空响应、非法 JSON、Schema mismatch、固定错误
  矩阵、单次 invoke 和 socket 阻断。审查修订聚焦回归为
  `217 passed in 19.79s`。
- 阶段 1A Runner 已在嵌套 `try/finally` 中临时强制 Mock 和空 DeepSeek Key，
  并按原变量是否存在精确恢复；pre/post Scope 使用 M12A，安全扫描增加 Provider
  敏感字段名。PowerShell parser 为 `RUNNER_PARSE_OK`。
- `check-scope.ps1` 已增加精确 M12A allowlist；当前初次门禁为
  `SCOPE_OK M12A`。五份设计基线、migration、公共 API Schema、Frontend、
  Mock Runtime 与 `SEM/` 均未修改。
- 最新正式阶段 1A Runner run id 为
  `20260728T141030Z-5d072a155237`：
  `PHASE_1A_ACCEPTANCE_PASSED`、`failed=0`、
  `browser_manual_scenarios=11`。M11 E2E 为 `24 passed`，Backend 全量为
  `978 passed`，Mock Runtime 为 `11 passed`，Frontend 为 9 files /
  `204 passed`；typecheck/build 均通过。Runner 动态 artifacts 安全扫描为
  `ACCEPTANCE_SECURITY_SCAN_OK`。
- Alembic head/current 均为 `0009_timeline_query_indexes`，check 通过；
  `SCOPE_OK M12A`。SEM 为 57 files、
  `total_size_bytes=2043071133`、fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
  Runner 动态 artifacts 安全扫描通过，并在 finally 中完成 Mock 栈精确清理。

## M12-A 审查修订执行记录

- 修订开始基线仍为
  `main@f5e24dcaab4801dbeffb8400f2960c33b60b4f00`，staging empty；
  22 个 tracked modified 与 7 个 untracked，共 29 个既有 M12-A 路径。
  本轮只新增修改两个此前未 dirty、但已位于 M12-A allowlist 的测试：
  `backend/tests/api/test_explanation_outcomes.py` 与
  `backend/tests/unit/test_explanation_service.py`。当前共 31 个路径，
  `SCOPE_OK M12A`。
- Chat `parsing_error` 现以内存 `json.loads(raw.content)` 区分非法 JSON 与
  合法 JSON 的 Schema 失败；直接 `JSONDecodeError` 固定为
  `LLM_INVALID_JSON`。`_domain_result()` 的外部数据断言已替换为显式校验；
  保持单次 invoke，不增加 repair、retry、fallback 或 raw content 持久化。
- `ExplanationOutcome` 增加可选内部 LLM 错误字段。DeepSeek 细分错误只进入
  `LLMCall`；`NaturalLanguageExplanation`、`Task` 和 HTTP API 继续使用公开
  `EXPLANATION_*` 错误。Mock 内部字段为 null 时，LLMCall 兼容回退到公开错误。
  DB 参数化用例覆盖 401/402/429/空响应，API 用例证明 401 细分类不进入公开响应。
- DeepSeek 五键 generation parameters 已与 purpose 绑定：
  Chat 仅接受 `json_object/1024`，Explanation 仅接受 `text/768`；
  两个交叉组合均拒绝。既有 Mock 两键形状保持兼容，无 migration。
- Explanation 对 CRLF/CR/LF/tab 先规范化为空格、折叠空白并 trim；
  其他 Unicode 控制字符、规范化后空文本和超过 4096 字符继续失败关闭，
  最终 Domain 文本保持可打印受控单行。
- 干净子进程以 Mock 配置 import main 并创建应用后，两个 DeepSeek adapter
  模块均不在 `sys.modules`；既有 fail-if-constructed 测试继续证明 Mock 模式
  DeepSeek construction=0。聚焦 socket 阻断测试为 `1 passed in 1.33s`；
  该结论只覆盖注入 fake adapter 的聚焦调用路径，不作完整进程级网络泛化。
- 修订 TDD RED 证据：Chat 分类 `3 failed, 41 passed`；Explanation 分层
  `17 failed, 45 passed`；purpose 绑定 `2 failed, 55 passed`；Key 首尾空白
  `2 failed, 52 passed`。对应实现后全部转绿。
- 最新权威 Runner 的 Backend full 为 `978 passed in 186.40s`，E2E
  `24 passed in 61.92s`，Mock Runtime `11 passed in 0.93s`，Frontend
  `204 passed in 4.38s`；Alembic head/current/check、Scope、SEM、
  Git checks、安全扫描全部通过。SEM fingerprint 仍为
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- Runner 结束后 3000/8000/8100 listener 为 0、Compose running service
  为空、Mock state 文件不存在。未删除命名 volume，未运行真实 Provider，
  未读取 `.env`，未暂存或 commit。

## M12-B 当前执行记录

- 开始门禁实时核对为
  `main@ffd29353cb4682c74fd3455425822999444bb1d2`，subject
  `feat: add offline DeepSeek provider integration`，parent
  `f5e24dcaab4801dbeffb8400f2960c33b60b4f00`；开始时 working tree、
  staging 和 untracked 均为空。
- 新增 PowerShell Runner 与非 pytest Python executor。`-SelfTest` /
  `--self-test` 不检查 `DEEPSEEK_API_KEY`，使用 fake Chat、Explanation 与
  Runtime，并在公网 socket 封锁下验证五次硬预算、第六次 delegate 前拒绝、
  Complete Tool 两次余额门槛、两类零增量重放、usage、request-id
  present/absent、失败停机、环境恢复和安全 summary。
- 真实模式具有双重授权门；缺少非空进程级 `DEEPSEEK_API_KEY` 或
  `M12B_REAL_CALLS_AUTHORIZED=YES` 时，在服务启动和网络调用前 fail closed。
  Runner 在内存保存 Key 后从父环境移除，以 `LLM_ADAPTER=mock` 启动本 Run
  独占的 PostgreSQL、MinIO 与 Mock Runtime；不启动 Frontend 或独立 Backend。
  只有 Python executor 子进程获得 Key 与 `LLM_ADAPTER=deepseek`，Key 不进入
  命令行、文件、stdout、stderr 或报告。
- executor 为每 Run 创建唯一 database、MinIO bucket、actor、conversation、
  idempotency key 与 artifact 目录；运行既有 Alembic migration，显式构造并
  分别包装 DeepSeek Chat / Explanation Port，通过 `create_app` 注入后使用
  FastAPI TestClient 从正式公共 API 进入。Tool 仍只调用 Mock Runtime。
- 固定真实预算为 Knowledge Chat 1、Complete Tool Chat 1 + Explanation 1、
  NEEDS_INPUT Chat 1、90 min 且缺 aging time 的受控 Chat 1，总计 5；
  Knowledge 与 Complete Tool 相同 key 重放必须使 Provider、Runtime 和全部
  业务资源增量为 0。M12-B 不执行真实 Explanation retry。
- 每个场景后同时核对线程安全 wrapper 计数、独立数据库中
  `deepseek/deepseek-v4-flash` LLMCall 数量与预期增量。成功 LLMCall 还核对
  purpose 对应模板、64 位 digest、精确 generation parameters、正 token
  usage 与 provider request-id 持久化。header 未暴露或被生产 Adapter
  过滤统一记录 `PROVIDER_REQUEST_ID_NOT_EXPOSED_OR_REJECTED`，不使用
  completion/framework ID 替代。
- `check-scope.ps1` 新增精确 M12B allowlist，仅含两个 Runner、Scope、
  `docs/acceptance/real-llm-provider.md` 与本进度文件；旧 milestone allowlist
  未改。正式报告当前仅为 `M12B_IMPLEMENTED_NOT_EXECUTED` 模板，不含真实结果
  或固定合成场景正文。
- Python executor `--self-test` 为 `M12B_EXECUTOR_SELF_TEST_OK`，
  PowerShell Runner `-SelfTest` 为 `M12B_POWERSHELL_SELF_TEST_OK`，
  PowerShell parser、`pip check`、`SCOPE_OK M12B` 与 `SEM_INTEGRITY_OK`
  均通过。
- 为避免旧 Phase 1A Runner 固定 `Scope M12A` 与当前未提交 M12-B 五路径
  产生伪冲突，本轮从当前空 staging 导出临时 HEAD 快照，并以不含
  `DEEPSEEK_API_KEY` 的最小子进程环境运行原 Runner；未修改真实 index、
  working tree 或旧 allowlist。相同的临时快照、junction 与成功后清理流程已
  内置于 M12-B 真实 Runner 的 Phase 1A 回归阶段。run id
  `20260728T164744Z-6c67a9b91214` 为
  `PHASE_1A_AUTOMATION_PASSED`、`failed=0`：M11 E2E `24 passed`，
  Backend full `978 passed`，Mock Runtime `11 passed`，Frontend
  9 files / `204 passed`，typecheck/build、Scope M12A、SEM、Git 与动态
  artifacts 安全扫描均通过。未声明本轮人工浏览器验收。
- Runner 后 Compose running service 为 0，3000/8000/8100 listener 为 0，
  Mock state 不存在，隔离快照已删除。当前真实 Provider calls=`0`；未读取或
  设置 API Key，未运行 Runner 真实模式，未暂存或 commit，未进入 M13。

## M12-B 审查修订执行记录

- 真实模式在任何服务启动前强制核对 branch、HEAD、subject、parent、两项
  tracked modified、三项 untracked、空 staging 和精确五路径集合；任一缺失或
  unexpected 均以 `M12B_GIT_PREFLIGHT_FAILED` 停止。
- Phase 1A snapshot 改到系统临时目录。cleanup 无条件优先删除 snapshot
  `.env`，再安全删除 SEM 与 node_modules junction；即使 state 残留也不保留
  `.env`，任何 snapshot 残留继续返回
  `M12B_PHASE1A_CLEANUP_FAILED`。M12-B artifact 不包含 `.env`。
- PostgreSQL、MinIO 与 Mock Runtime 启动前，父进程备份并移除 Key、授权门、
  model、base URL、timeout 五项 Provider 变量并强制 Mock；只有 executor
  child 获得固定 Provider 环境，`finally` 按原存在性和值逐项恢复父环境。
- Python executor 只在 with 内形成 pending summary；场景、metadata 与 DB
  安全扫描完成并成功退出资源 context 后才写 `status=PASSED`。cleanup exit
  失败写受控 FAILED summary，不保留 PASSED。
- 资源 count 与 ID 集合现纳入 IdempotencyRecord、TaskInputRevision 和
  ResultAssetLink；Knowledge 与 Complete Tool replay 必须保持
  IdempotencyRecord count 和 ID 集合不变。
- Port 可观察到的 null request-id 统一记录为
  `PROVIDER_REQUEST_ID_NOT_EXPOSED_OR_REJECTED`；Port 无法区分 header 未暴露与
  非法 header 被生产 Adapter 过滤，非法 header 拒绝引用 M12-A 离线合同证据。
- 统一 Secret scan 忽略空值，覆盖 Key、PostgreSQL password、MinIO secret、
  Runtime token 和 Timeline signing key。Phase 1A 三项 artifacts 生成后再做
  最终 artifact scan；只有最终扫描通过才输出真实验收终态 marker。
- failure summary 将 `status`、`safe_error_code` 与 `security_scan` 分开；
  security scan 只取 `PASS`、`FAILED`、`NOT_COMPLETED`，普通业务失败不再误记
  为 Secret 泄漏。
- 审查修订 RED：原 Python self-test 以
  `M12B_SELF_TEST_REPLAY_RESOURCE_SET_INCOMPLETE` 退出 1；原 PowerShell
  self-test 因缺少 Provider 隔离/snapshot cleanup 行为退出 1。最小实现后
  Python 与 PowerShell self-test 均转绿；真实 Provider calls 保持 `0`。
- Phase 1A snapshot cleanup 进一步以真实 junction RED 锁定 Windows
  PowerShell 的 reparse-point 删除差异；实现先验证 ReparsePoint，再以非递归
  方式删除 junction 但保留目标。snapshot 树在 junction/state 门槛通过后以
  同进程同步删除，最多 30 秒按路径 absent 条件轮询；cleanup artifact 只记录
  `.env`、junction、state、snapshot 与 helper result 五项布尔值。
- 直接在无 Mock Stack 的 shell 中运行 Backend full / integration 时分别达到
  10 分钟和 5 分钟工具上限；定位到 PostgreSQL fixture 等待而非测试断言失败，
  并按精确 PID/command 清理本轮 pytest/Conda 进程。随后只通过正式 Phase 1A
  Runner 启动受控栈，不用扩大 timeout 掩盖环境前置条件。
- 最终权威 Phase 1A run id 为
  `20260729T034211Z-c1b66e05f59c`：
  `PHASE_1A_AUTOMATION_PASSED`、`failed=0`。Runner 执行 Backend E2E、
  Backend full、Mock Runtime、Frontend、pip、Alembic、Scope、SEM、Git 与
  动态安全扫描；Backend 当前收集 978 tests。另行离线证据为 Unit
  `469 passed`、Contract `147 passed`、DeepSeek wiring `5 passed`、
  Mock Runtime `11 passed`。最终 cleanup 与 Phase 1A artifacts 加入后的统一
  scan 均通过。

## M12-B 最终收敛修订执行记录

- 本轮对两个验收脚本做职责收敛：PowerShell Runner 负责 Git/文件基线、
  双重授权、Provider 环境隔离、Compose/Runtime 生命周期、外部进程 timeout、
  executor 启动、fallback cleanup、Phase 1A 回归、最终 artifact scan 和
  overall `summary.json`；Python executor 负责四个公共 API 场景、五次预算、
  provider-call ledger、真实 Adapter 包装、独立数据库/MinIO 生命周期、
  业务资源/幂等/metadata 检查、`executor-summary.json` 和 `--cleanup`。
- `provider-call-ledger.json` 由 PowerShell 在 executor 启动前创建；Python
  `ProviderCallBudget` 每次 delegate 前在锁内先写账本并读回确认，写入失败、
  第六次调用或场景停止均不会进入真实 Adapter。账本只保存 run id 与
  chat/explanation/total delegate attempts，不保存 Prompt、Key、模型响应、
  异常正文或用户消息。
- 调用次数语义已收敛：PowerShell 无论 executor 成功、受控失败、非零退出、
  timeout 或被终止，都会读取账本并尝试读取 `executor-summary.json`；
  `call_count_state` 只允许 `EXACT`、`CONSERVATIVE_UPPER_BOUND` 和 `UNKNOWN`。
  整体 PASSED 必须为 `EXACT` 且 `real_provider_calls=5`、
  `provider_delegate_attempts=5`。失败和 timeout 不再伪造为 0；账本缺失或损坏
  固定为 `UNKNOWN`。
- Python `executor-summary.json` 的 FAILED 结构包含 `summary_authority`、
  `status`、`safe_error_code`、`security_scan`、`real_provider_calls`、
  `chat_delegate_attempts`、`explanation_delegate_attempts` 和
  `total_delegate_attempts`。PowerShell 只在结构合法且与账本一致时传播其
  safe error、security state 和计数；summary 缺失、损坏、timeout 或账本损坏
  使用 Runner 静态错误码。
- 新增 Python `--cleanup`，该模式不要求也不读取 Provider 授权变量，不导入
  DeepSeek Adapter，不执行 Provider 调用；仅按严格 run id 正则清理
  `materialsagent_m12b_<run-id>` 和 `materialsagent-m12b-<run-id>`，并确认
  database/bucket 不存在。PowerShell 在 executor 成功、失败、timeout 或被终止
  后、PostgreSQL/MinIO 仍运行时执行一次无 Provider 环境的 fallback cleanup。
- 所有本轮列出的可能阻塞外部操作均统一走 `Invoke-M12BChildProcess` timeout：
  pip check、Python self-test、真实 executor、cleanup executor、Phase 1A
  Runner、Docker Compose config/up/ps/stop、Scope 和 SEM。Compose `ps` 检查和
  `stop` 也已改为本 Runner 的 timeout helper，不再调用旧的无 timeout Compose
  helper。
- 整体状态权威层级已拆分：Python 只写
  `executor-summary.json`，其 PASSED 仅证明 Provider 业务场景和 executor
  独立数据库/MinIO bucket cleanup；PowerShell 的 `summary.json` 是唯一整体
  权威状态，仅在 executor、Runtime/Compose/ports cleanup、父环境恢复、
  Phase 1A、snapshot cleanup 和最终 artifact scan 全部通过后才能写 PASSED。
- Phase 1A 成功或失败后，只要产生 stdout、stderr、summary、cleanup 四项
  artifact，均进入最终统一扫描；Phase throw 不再越过扫描。发现实际 Secret
  时不输出匹配值，删除本 Run 的整个污染 artifact 目录，只在 M12-B state
  root 留存静态无 Secret failure marker。
- Complete Tool 验收只接受 Task 与 ToolResult 双 `SUCCEEDED`，双输出
  requested/completed 集合精确匹配且 failed 为空；同时要求单一 artifact、
  Asset/ResultAssetLink/MinIO object/Explanation 均精确增量 1，并核对
  `(result_id, artifact.asset_id)` 位于 ResultAssetLink ID 集合。
- 所有 Runner 子进程均有 wall-clock 上限：pip 与 Python self-test 为 120
  秒、真实 executor 为 900 秒、Phase 1A Runner 为 1200 秒；超时只终止精确
  Process 及其子进程树并进入外层 cleanup，不再无限 `WaitForExit()`。
- Python 在 SelfTest 和真实模式首次 Provider 调用前离线 import 实际
  Application/ORM/Runtime client 模块，核对七个 ORM ID 字段、
  `build_tool_registry` callable 和 Local Runtime adapter 构造签名；该
  preflight 不连接网络、数据库或 MinIO，失败固定为
  `M12B_REAL_EXECUTOR_INTERFACE_PREFLIGHT_FAILED`。
- Docker Compose `up` 前使用已移除 Provider 变量的受控环境运行
  `compose config --format json`，只在内存解析 postgresql/minio 最终环境；
  Provider 字段或实际 Key 值出现均在服务启动前以静态错误失败关闭，不输出
  完整 Compose config。
- 本轮 TDD RED 为 Python
  `M12B_SELF_TEST_FINAL_REVIEW_HELPERS_MISSING`、PowerShell
  SelfTest 非零退出；新增严格投影、接口、timeout、Compose、Phase 失败扫描、
  污染删除和整体 summary 门槛后，两项 SelfTest 均转绿。真实 Provider calls
  继续为 `0`，API Key 未读取，真实模式未执行。
- 最终离线回归使用空 staging/HEAD 的系统临时 snapshot，权威 run id
  `20260729T044121Z-9d97546179b1` 为
  `PHASE_1A_AUTOMATION_PASSED`、`failed=0`；该 Runner 覆盖 Backend full、
  Backend E2E、Mock Runtime、Frontend、pip、Alembic、Scope、SEM、Git 和
  动态安全扫描。Phase stdout/stderr/summary/cleanup 四项 artifact 均通过
  最终扫描，cleanup 的 `.env`、junction、state、snapshot 和 helper result
  五项布尔值均为 true，stderr 为空；随后验证临时 driver/artifact 已删除、
  六个相关端口已释放。
- 最终新鲜局部门槛为 PowerShell parser、Python compile、
  `M12B_EXECUTOR_SELF_TEST_OK`、`M12B_POWERSHELL_SELF_TEST_OK` 和
  `pip check` 全部通过。PowerShell timeout SelfTest 实际启动父/子两级受控
  sleep 进程，短 timeout 后两级 PID 均无残留。
- 本轮最终收敛后的新鲜验证为：PowerShell parser `RUNNER_PARSE_OK`、Python
  compile PASS、Python executor `--self-test` 输出
  `M12B_EXECUTOR_SELF_TEST_OK`、PowerShell Runner `-SelfTest` 输出
  `M12B_POWERSHELL_SELF_TEST_OK`、Backend `pip check` 输出
  `No broken requirements found.`、`SCOPE_OK M12B`、`SEM_INTEGRITY_OK`、
  `git diff --check` 和 `git diff --cached --check` 均通过。真实 Provider
  calls 仍为 `0`，API Key 未读取，真实模式未执行。
- 本轮 Phase 1A 回归使用系统临时 HEAD snapshot 和受控本地栈 `.env` 键，
  不读取或传递 DeepSeek API Key；权威 run id
  `20260729T070717Z-0dcd6c53dfe9` 为
  `PHASE_1A_AUTOMATION_PASSED`、`failed=0`、dynamic artifacts scanned=true。
  其中 Backend E2E `24 passed in 62.09s`，Backend full
  `978 passed in 185.86s`，Mock Runtime `11 passed in 0.96s`。该临时 snapshot
  及先前 `.env.example` placeholder 预尝试目录均已删除；placeholder 预尝试因
  Alembic 连接本机既有 Docker volume 配置失败，未进入业务测试，不作为验收
  通过证据。

## M12-B 静态扫描确定性误报修订记录

- 根因是 Git diff 与正式报告沿用了动态证据的 `-StrictOutput` 调用方式；报告中
  合法的 `Dual authorization:` 已命中 `authorization:` 严格标记。底层实际
  Secret 值比较没有缺陷，误报来自扫描调用职责混用。
- TDD RED 在只增加 PowerShell SelfTest 后稳定输出
  `M12B_POWERSHELL_SELF_TEST_STATIC_REPORT_FALSE_POSITIVE` 并退出 1；失败发生
  在直接读取当前正式报告并按动态严格策略扫描时。
- PowerShell Runner 现以 `Test-M12BDynamicEvidenceText` 保持 runtime
  artifacts/stdout/stderr 的实际 Secret 与严格标记扫描，以
  `Test-M12BStaticReviewedText` 对 Git diff 和正式报告仅比较非空实际 Secret
  值。环境变量名称、安全术语和拒绝规则名称本身不视为泄漏。
- SelfTest 直接读取正式报告，确认包含 `Dual authorization:`、`Bearer`、
  `reasoning_content` 与 `response headers` 时静态扫描通过；临时副本注入
  canary Secret 后静态扫描失败；动态文本中的 `Authorization:`、
  `reasoning_content` 和同一 Secret 值均继续失败。
- GREEN 为 PowerShell Runner `-SelfTest` 输出
  `M12B_POWERSHELL_SELF_TEST_OK`。本轮未修改 Python executor 的业务场景、
  调用预算、账本、数据库、MinIO 或清理逻辑。
- 本轮新鲜离线门槛为 PowerShell parser `RUNNER_PARSE_OK`、Python compile
  `PYTHON_COMPILE_OK`、Python executor `M12B_EXECUTOR_SELF_TEST_OK`、
  PowerShell Runner `M12B_POWERSHELL_SELF_TEST_OK`、Backend `pip check`
  `No broken requirements found.`、`SCOPE_OK M12B`、`SEM_INTEGRITY_OK`、
  `git diff --check` 与 `git diff --cached --check` 全部通过。
- 正式 Phase 1A Runner 使用隔离 HEAD snapshot 完成回归，权威 run id
  `20260729T081820Z-b387f2c4696b`，状态
  `PHASE_1A_AUTOMATION_PASSED`、`failed=0`、
  `dynamic_artifacts_scanned=true`。cleanup 的 `.env`、junction、state、
  snapshot 与 helper result 五项均为 true，隔离驱动目录随后已删除。
- 本轮 API Key 未读取，真实 Provider calls=`0`，真实 M12-B 模式未执行；
  staging、commit、push、amend 与 M13 均未授权。

## M12-B 真实浏览器验收与最终收尾记录

- 项目负责人已通过正常本地开发栈完成浏览器人工验收：Backend 使用 DeepSeek，
  PostgreSQL/MinIO 为本地服务，Tool Runtime 保持 Mock，Frontend 浏览器作为
  主要业务入口；未运行真实 SEM 模型。
- 知识问答成功并返回真实自然语言材料知识回答，未触发 Tool。NEEDS_INPUT 场景
  成功，`missing_fields` 为 `aging_temperature`、`aging_time`，未执行 Tool。
  分钟单位场景将 `90 min` 规范化为 `1.5 h`，仍缺 `aging_time`、保持
  `NEEDS_INPUT`，未执行 Tool。
- 完整 Tool 场景最终 Task=`SUCCEEDED`，图片、力学性能结果和自动 Explanation
  均存在，页面无安全错误；Tool Runtime 为 Mock Runtime，真实 SEM 模型未运行。
- 数据库审计确认 DeepSeek LLMCall 共 6 条且全部 `SUCCEEDED`：
  `CHAT_ORCHESTRATION=5`、`TOOL_RESULT_EXPLANATION=1`；provider/model、
  Chat/Explanation template、prompt digest、token usage 与 generation
  parameters 均符合固定配置，非法 digest、缺失/非法 usage 和 error_code
  记录均为 0。
- 计划业务验收调用为 5 次，人工验收过程中额外追加“你是谁”知识问答 1 次，
  因此累计观察值如实记录为 6，不改写为 5。本轮最终收尾新增真实 Provider
  调用为 0。
- 6 条记录均未暴露 `x-request-id`，`provider_request_id=null`；未使用
  completion ID、LangChain ID、Task ID 或应用 request ID 替代。
- 两个未采用的 untracked 自动 real-provider Runner 已删除，不进入最终提交。
  M12B Scope 已收敛为正式报告、本进度文件与 `check-scope.ps1` 三路径；此前
  Runner 段落仅为未采用实现的审查历史，不代表自动 Runner 完成真实验收。
- 项目负责人确认本地配置已恢复 `LLM_ADAPTER=mock` 且 API Key 已清空。本轮
  不读取 `.env`，不再次调用 DeepSeek。
- 最终 Phase 1A Mock 回归：run id
  `20260729T095845Z-1fc7cbda8a8d`，`PHASE_1A_AUTOMATION_PASSED`、
  `failed=0`、`dynamic_artifacts_scanned=true`；Backend E2E `24 passed`、
  Backend full `978 passed`、Mock Runtime `11 passed`、Frontend 9 files /
  `204 passed`，Alembic、pip、typecheck/build、Scope M12A、SEM、Git 与动态
  安全扫描均通过。
- 最终 Scope / SEM / Git / 三路径安全扫描：`SCOPE_OK M12B`、
  `SEM_INTEGRITY_OK`、精确 3 路径、空 staging、两项 diff check 与安全扫描
  均为 `PASS`；3000/8000/8100 无监听残留。

## 已知风险

- P1B2 门二只证明四份固定真实权重和完整 bundle 的 CPU 加载兼容性；没有证明
  DDPM 采样、模型 forward、SVR predict、GPU 显存承载或真实性能预测。实际 GPU
  仍为 6 GiB RTX 3060 Laptop，相关风险必须留到另行授权的门三验证。
- DenseNet 的 121 个旧 BatchNorm counter 依赖 PyTorch 1.13.1 内置版本兼容路径；
  compatibility 测试已锁定精确集合和 strict=True 结果，生产 loader 没有放宽。
- 正式 loader 是无状态工厂；为遵守“不创建第二套完整模型对象”，本门未执行同一
  loader 的第二次完整 load。`LoadedModelBundle` 没有 `is_loaded()` API；已验证
  单个 bundle 的内部 `_closed` 在 close 后为 true，并在持有者释放后清理可验证弱引用。
- 原始 Base 审计未保留开始／结束包数组快照，无法逐字段复原旧哈希差异；后续已
  通过日志、稳定语义投影、revision 和元数据时间确认其为审计算法误报，Base 包
  集合未变化。
- `npm ci` 仍报告既有 lockfile 的 6 个 high severity audit 项；P1B1 禁止修改
  Frontend 依赖，未执行 `npm audit fix`。
- M12-B 浏览器验收已证明真实 Provider 正常业务链路；真实错误分类和幂等合同
  仍引用 M12-A 离线自动测试，不声称本轮通过额外真实错误调用验证。
- Provider 在 6 次成功调用中均未暴露 `x-request-id`；该事实作为能力观察保留，
  不使用其他标识符替代。
- 阶段 1A Runner 依赖 Windows PowerShell、Docker Desktop/Compose 和本机
  Backend/Frontend 工具链；它不是生产守护进程。
- start/stop 继续以严格 ownership 为先；不得宽泛终止进程或删除 volume。
- P1B2 门二完整 CPU bundle 已成功加载和关闭；Module `__call__`、任何直接
  forward、SVR predict、PCA transform、fit、score、DDPM 采样和 GPU 模型迁移
  均未执行。

## 下一步

停在 P1B2 门二验收提交完成点。未经新的项目负责人明确授权，不得开始门三，不得
设置 GPU 授权变量，不得执行模型 forward、SVR predict、DDPM、真实 Runtime、
Backend、MinIO、浏览器或 DeepSeek 综合验收。不得 push、amend 或创建第二个提交：

```text
当前里程碑：P1B2
当前工作单元：P1B2 门二：真实权重加载兼容性
状态：COMPLETE / PROJECT_OWNER_ACCEPTED

M11:
COMPLETE / PROJECT_OWNER_ACCEPTED
acceptance commit:
f5e24dcaab4801dbeffb8400f2960c33b60b4f00

Phase 1A:
COMPLETE / PROJECT_OWNER_ACCEPTED

M12:
COMPLETE / PROJECT_OWNER_ACCEPTED

M12-A:
COMPLETE / PROJECT_OWNER_ACCEPTED

M12-B:
COMPLETE / PROJECT_OWNER_ACCEPTED

Real Provider calls:
6 observed LLMCalls
- 5 planned acceptance calls
- 1 additional manual knowledge call

P1B1:
COMPLETE / PROJECT_OWNER_ACCEPTED

P1B2 门一:
COMPLETE / PROJECT_OWNER_ACCEPTED
acceptance commit:
c86c8eddfbb7a4b7354dd2299465cf352530623a

P1B2 门二:
COMPLETE / PROJECT_OWNER_ACCEPTED

P1B2 门三:
NOT STARTED / NOT AUTHORIZED

P1B2 门四:
NOT STARTED / NOT AUTHORIZED

Staging:
EMPTY AFTER ACCEPTANCE COMMIT

Commit:
ONE ACCEPTANCE COMMIT AUTHORIZED / HASH REPORTED AFTER GIT CREATION

Push:
NO

Amend:
NO
```
