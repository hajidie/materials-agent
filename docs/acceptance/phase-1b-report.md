# Phase 1B 验收报告

## 一、恢复基线

- 本工作单元：P1B2 门一——独立环境与依赖验证。
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

## 九、已知限制

- 当前只验收独立环境、候选依赖、CUDA 基础可用性和 Runtime 离线测试。
- 三个 compatibility 测试仍由真实授权门明确跳过；四份权重尚未通过真实加载
  兼容性检查。
- 未验证真实 DDPM/DenseNet/SVR 构造、最小 GPU 推理、资源峰值、真实 Runtime
  生命周期、Backend/MinIO 接入或浏览器 E2E。
- RTX 3060 Laptop GPU 的 6144 MiB 显存可能约束后续固定参数推理；必须在独立
  授权门中测量，不能由 CUDA 基础 Tensor 结果推断。
- 原始审计未保留开始／结束包数组快照，无法逐字段复原旧哈希差异；后续已通过命令
  日志、稳定语义投影、revision 和元数据时间确认该差异为审计算法误报。

## 十、下一授权门

- P1B2 门一：`COMPLETE / PROJECT_OWNER_ACCEPTED`。
- P1B2 门二：`NOT STARTED / NOT AUTHORIZED`。
- 当前尚未到达或执行权重加载授权点；P1B2 门二仍需项目负责人另行明确授权。
- 未经新的明确授权，不得设置真实授权变量、打开权重、调用真实 loader、构造
  模型、执行 DDPM、启动真实 Runtime，或调用 Backend、MinIO、DeepSeek。
- 本轮不执行 git add、commit、push 或 amend。

最终状态：`P1B2_ENVIRONMENT_GATE_PROJECT_OWNER_ACCEPTED`。
