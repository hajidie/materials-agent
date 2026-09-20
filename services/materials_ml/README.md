# Materials ML Engine 与独立 Service

独立的 Python 3.11 数值表格单目标回归包，当前仅实现 LR/RF。它不导入 Flask、FastAPI、MCP、Agent、
数据库、MinIO 或旧 `index.py`。同安装包中的 `materials_ml_service` 提供独立 Resource API、Windows Worker、Prediction 和可选 MCP 入口；
领域边界见 [项目上下文](../../docs/project-context.md#materials-ml-领域边界)。平台可显式启用可信资源上下文，
通过现有 Agent/MCP 提交训练；默认关闭。平台仅提供聊天内附件、结果卡片与 Viewer，不建设独立 ML 管理 UI。

## 安装与验证

在仓库根目录使用一个 Python 3.11 解释器创建隔离环境；不要启用 system-site-packages：

```powershell
py -3.11 -m venv services/materials_ml/.venv
& ./services/materials_ml/.venv/Scripts/python.exe -m pip install -e './services/materials_ml[dev]'
& ./services/materials_ml/.venv/Scripts/python.exe -m pip check
& ./services/materials_ml/.venv/Scripts/python.exe -m pytest services/materials_ml/tests/test_engine.py services/materials_ml/tests/test_package.py services/materials_ml/tests/test_isolation.py -q
& ./services/materials_ml/.venv/Scripts/python.exe -m build --wheel --outdir services/materials_ml/dist services/materials_ml
```

也可使用 `environments/materialsagent-ml.yml` 创建独立 Conda 环境后安装本包。直接运行依赖与构建后端版本
固定在 `pyproject.toml`，不安装进 Backend 或 ZTA35G Runtime 环境。上述 Engine 测试只使用合成数据与临时目录，
无需 LLM、Docker、GPU、PostgreSQL 或 MinIO。Service 的额外安装和真实验收见下文。
环境、缓存和构建产物由仓库现有规则忽略。

## Python 接口

```python
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from materials_ml import (
    TrainingSpec, analyze_table, train_regression, predict,
    save_package, load_package, evaluate_test,
)

x = np.arange(60, dtype=float)
data = pd.DataFrame({"composition": x, "strength_MPa": 2.5 * x + 4})
diagnostics = analyze_table(data)
spec = TrainingSpec(
    features=("composition",), target="strength_MPa", algorithm="LR",
    units={"strength_MPa": "MPa"},  # 可省略；列名本身不产生单位
)
model = train_regression(data, spec)
assert model.report["test"]["metrics"]["r2"] > 0.999999

with TemporaryDirectory() as folder:
    location = save_package(model, Path(folder) / "model")
    # 只对本 Engine 生成且调用方已确认来源的包设置 trusted=True。
    restored = load_package(location, trusted=True)
    output = predict(restored, data[["composition"]])
    np.testing.assert_allclose(output.values, predict(model, data[["composition"]]).values,
                               rtol=1e-10, atol=1e-12)
    assert evaluate_test(restored, data) == model.report["test"]
```

| 接口 | 输入与结果 |
|---|---|
| `read_csv(bytes)` | UTF-8/UTF-8 BOM、逗号分隔、标准引号 CSV；预检唯一非空表头与等宽行，再由 pandas 解析数值及常见缺失标记；不接受路径/URL，不静默重命名重复列 |
| `analyze_table(DataFrame)` | 返回完整输入身份、列类型/缺失/常量诊断、非数值列、重复行数及 IID 假设；不猜测目标或单位 |
| `TrainingSpec(features, target, algorithm="LR", test_size=0.2, random_state=42, units={})` | 显式选择数值列与单目标；算法仅 LR/RF，无参数覆盖/搜索入口 |
| `train_regression(table, spec)` | 返回 `ModelPackage(pipeline, manifest, report, splits)`；输入表不修改 |
| `validate_training_input(table, spec)` | 与训练共用纯预检并返回绑定数据身份的划分；不 fit，用于 Service 提交门禁 |
| `predict(package, table)` | 只接收保存的全部特征列，可重排；缺列、多列、非数值拒绝；返回 `PredictionResult(values, row_positions, target, unit)` |
| `table_identity(table)` / `validate_split_binding(table, splits)` | 创建或检查完整数据内容身份，历史索引绑定错误抛出 `EngineError` |
| `evaluate_test(package, original_table)` | 先验证完整输入身份，再重放原 Test 预测和四项指标；不 fit、不重新选取 Test |
| `save_package(package, new_directory)` / `load_package(directory, trusted=True)` | 临时包校验后发布到新目录；完整性/兼容性校验后加载；不覆盖已有目录 |

选择列接受实数数值 dtype，拒绝布尔、复数、字符串或类别编码；内部转 float64。未选的字符串/类别列保留
在诊断与内容身份中。表只支持标量数值、文本、布尔和缺失单元格，暂不支持嵌套对象或日期型单元格。
CSV 解析结果的 dtype 也是输入身份的一部分。预测允许新的数值 dtype，但必须能表示成有限 float64，
特征缺失沿用训练中位数；预测不做单位推断/匹配或任何重新拟合。

## 训练与评估合同

流程为输入验证 → 外部 Train/Test → Train 内打乱的固定 5-fold → 外部 Train 最终拟合 → Test 评估。
LR：中位数填补、StandardScaler、带截距 LinearRegression；RF：中位数填补、100 棵树、固定种子、n_jobs=1。
每折独立 clone/fit 完整 Pipeline。模型始终对应所报告的外部 Test 评估，不做全量重训；y 不变换。

开始拟合前拒绝目标缺失、无穷值、重复列名，以及任一拟合分区内全空特征。Test 与每个 validation 均至少
2 行，且每个计算 R² 的分区目标非常量。不足时不自动降折、改比例或用 0/1 替代未定义 R²。
默认比例下至少需要 13 行才有可能满足样本量条件，实际内容还须通过常量目标与缺失检查。
所有指标用 `metric(y_true, y_pred)`；训练、Test 与逐折都有 R²/MAE/MSE/RMSE，CV 汇总采用各折的非加权
均值与总体标准差（ddof=0）。非有限指标直接失败，不在 JSON 中输出 NaN/Infinity。

单位只保留调用者提供的映射；例如列名 `strength_MPa` 不会自动产生 `MPa`。数值尺度始终与目标输入相同；
MSE 的数值是误差平方，单位元数据表示原始列单位，当前不生成派生单位字符串。
不验证 IID，不自动去重；重复行有警告。分组、时间序列、类别编码、参数搜索、自动模型选择、其他算法、
SHAP/PCA/t-SNE、图表、设计优化、不确定性与深度学习均不在此包当前能力内。

## 数据身份与包格式

`table-json-v1` 对以下 UTF-8 JSON 逐条追加换行后计算 SHA-256：首先是固定字段顺序的
`version, columns, dtypes, row_count` 对象，随后每一行单元格数组（紧凑分隔符、原列/行顺序、非 ASCII 不转义）。
数值转 Python 标量；缺失统一为 null，拒绝无穷值；dtype 保留字符串表达，category 另记录类别清单、类型及
ordered 标记。DataFrame index 标签不入摘要，因此重复标签不影响原始零基位置。字节编码格式不同的 CSV
可以产生相同表内容身份；Service 另外记录原始文件 SHA-256。

`splits.json` 保存 dataset 的 sha256/fingerprint_version/row_count、外部 train/test 和每折 train/validation。
所有索引都是完整输入表的位置，CV 索引不是 Train 子表的局部编号。验证器检查范围、互斥、覆盖关系；包加载
还按记录的 seed/test_size/5-fold 重建并比较完整索引。历史评估须使用完整原始表，即使只改变未选列也拒绝。

目录格式为 `materials-ml-package-v1`：

```text
manifest.json       # 数据/特征/目标/单位/配置/依赖/完整 estimator 参数与文件清单
pipeline.joblib     # 完整 fitted Pipeline，不按文件名分别关联 scaler/estimator
evaluation.json     # 逐折/训练/Test 指标、原始行位置、对应 y_true/y_pred 和诊断
splits.json         # 绑定数据身份的全部划分索引
```

manifest 的 files 清单记录其他三个文件各自的 SHA-256、size_bytes 和 media_type；manifest 不自引用摘要。
参数从拟合后的 estimator 完整 `get_params(deep=True)` 获取；预处理参数也保存，特殊浮点参数（如 imputer 的
missing_values=NaN）用显式对象编码，而不是输出非标准 JSON 数字。manifest 不包含权重路径或平台凭据。
评估文件含目标值和预测值，应与原数据同等控制访问，不发给 LLM 或写入平台日志。

加载严格匹配 Engine/包格式，以及记录的 Python patch、NumPy、pandas、SciPy、scikit-learn、Joblib、
threadpoolctl 版本。当前没有跨版本迁移；版本不一致时需在原环境加载或重新训练。
先校验成员/大小/哈希/配置/指标/索引，再反序列化已经校验的 bytes，最后检查 Pipeline 类型、拟合与参数合同。
JSON 单文件上限 32 MiB，模型文件上限 256 MiB；发布时也执行相同检查。

Joblib/Pickle 只用于可信自产包。完整性清单可检测损坏，不能防止攻击者同时替换文件与清单；
`trusted=True` 是调用者提供的来源保证，不是安全扫描。不要加载上传的任意模型包。
保存只发布已验证的新目录，失败清理自己创建的临时目录；不承诺断电后的磁盘持久性或跨进程包写入协调。

`EngineError.code` 是稳定的机器可读拒绝原因（如 INSUFFICIENT_SAMPLES、CONSTANT_TARGET_PARTITION、
DATASET_MISMATCH、PACKAGE_INTEGRITY_ERROR）。调用方负责把它映射成服务错误；Engine 不创建 TrainingRun
状态、不执行取消/重试，也不依赖网络服务。

## 独立 Service 安装与启动

Service 和 Worker 在 Windows/Python 3.11 独立环境中运行。根 `.env` 启用任一 ML 功能时，
平台 `scripts/dev/local-dev.ps1` 会在配置和独立资源已经 provision 后执行迁移并托管这两个进程；
下面的命令仍可用于独立启动和诊断。MCP 在独立 Service 内提供入口；Backend 执行适配不导入本包。共享的是物理 PostgreSQL/MinIO 实例，
使用独立数据库、角色、bucket 与 IAM 凭据。不要把平台数据库或 MinIO 管理员凭据写进 ML 运行配置。

在仓库根目录安装 Service、HTTP Worker 和小型存储引用包：

```powershell
& ./services/materials_ml/.venv/Scripts/python.exe -m pip install -e ./packages/materials_storage -e './services/materials_ml[service,worker,mcp,dev]'
Copy-Item services/materials_ml/.env.example services/materials_ml/.env
```

仅首次创建配置时复制模板，已有配置直接编辑。填写独立数据库密码、MinIO 密钥，以及 Resource/Worker 两个不同的
32–256 字符随机 URL-safe token。启用 MCP 时设置 ML_MCP_ENABLED=true 和第三个不同的 ML_MCP_TOKEN。数据库 URL 的密码须 URL 编码。该 .env 已忽略，不提交。
独立配置只在显式设置 ML_ENV_FILE 时读取，不会自动加载平台根 .env。
ML_MINIO_ENDPOINT 是 host:port，没有 http:// 前缀；服务监听默认 127.0.0.1:8200。

先确认仓库现有 PostgreSQL/MinIO 实例可用；如需启动，仅执行：

```powershell
docker compose up -d postgresql minio
```

首次初始化在专用管理员终端进行。将以下管理员环境变量从本机已有配置安全赋值，
不要把真实值写进命令历史、日志或版本库：
ML_ADMIN_DATABASE_URL（现有 PostgreSQL 管理连接）、ML_ADMIN_MINIO_ACCESS_KEY、ML_ADMIN_MINIO_SECRET_KEY。
它们仅供 provision，运行服务和 Worker 不需要。

```powershell
$env:ML_ENV_FILE = (Resolve-Path services/materials_ml/.env).Path
& ./services/materials_ml/.venv/Scripts/python.exe -m materials_ml_service.admin provision
if ($LASTEXITCODE -ne 0) { throw 'ML provisioning failed; inspect the explicitly named resources before continuing.' }
Remove-Item Env:ML_ADMIN_DATABASE_URL, Env:ML_ADMIN_MINIO_ACCESS_KEY, Env:ML_ADMIN_MINIO_SECRET_KEY -ErrorAction SilentlyContinue
& ./services/materials_ml/.venv/Scripts/python.exe -m materials_ml_service.admin migrate
if ($LASTEXITCODE -ne 0) { throw 'ML migration failed.' }
& ./services/materials_ml/.venv/Scripts/python.exe -m materials_ml_service.api
```

provision 只接受全新 materials_ml 数据库/角色和 materials-ml bucket；
验收专用名称为 materials_ml_ 加 16 位随机十六进制。已有身份一律拒绝覆盖。
provision 跨数据库和对象存储并非原子操作；中断后可能留下部分已创建资源，须按这些明确身份核对，
不能反复初始化或删除共享数据。成功后不再运行 provision，后续只显式 migrate 再启动。
服务启动不自动建表。迁移包含在 wheel 中，独立于 Backend Alembic。

在另一个干净终端启动 Worker，只提供下面两个变量；token 应与 Service 配置中的 Worker token 一致：

```powershell
$env:ML_SERVICE_URL = 'http://127.0.0.1:8200'
$env:ML_WORKER_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'Worker token' -AsSecureString)).Password
& ./services/materials_ml/.venv/Scripts/python.exe -m materials_ml_service.worker
```

Worker CLI 检测到继承的 ML 数据库、MinIO、Resource 或管理员凭据会拒绝启动。
--once 只领取并处理一个任务，空队列则退出。停止 Worker 会停止当前受控训练子进程；
重启后先确认旧 Job 进程树消失，再将旧任务收敛失败/取消，不自动重新训练。
进程确认失败时保持待恢复并拒绝新任务。重新训练须新 Idempotency-Key。

GET /health/live 仅检查进程；GET /health/ready 检查数据库和 bucket 可达。
Service 启动及每 5 秒执行有界对账/清理；也可执行
`python -m materials_ml_service.admin maintain` 做一次维护。没有 Redis/Celery 或 MCP Tasks。
Service、Worker 都只支持本机可信使用，token 是 Resource/MCP/Worker 三个角色的凭据，不是用户账户或 scope 所有权凭据。

## Resource API

所有资源请求使用 `Authorization: Bearer <Resource token>`，基础路径
`/api/v1/scopes/{scope_id}`。scope 是无业务含义的不透明标识，所有血缘必须同 scope。
公开响应不含对象 key、内部路径或 claim；仅内部 Worker API 接受 Worker token。
路由合同如下表；当前不开放 /docs 或 OpenAPI 下载。独立客户端可直接 GET TrainingRun；平台聊天 UI
通过 Backend 的受控资源代理与结果观察流程访问，不直接连接 ML Service，也不靠 Viewer 读取触发 Agent。

| 方法与相对路径 | 用途 |
|---|---|
| POST /datasets | multipart file + 可选 metadata JSON（display_name、units）；需 Idempotency-Key |
| GET /datasets，GET /datasets/{id} | 列表/详情；列表支持 limit（1–100）、after 游标 |
| GET /datasets/{id}/content | 已发布的原始 CSV |
| DELETE /datasets/{id} | 无任何 TrainingRun/Prediction 血缘时进入异步删除，202 不表示对象已清完 |
| POST /training-runs | dataset_id、features、target、algorithm=LR/RF、test_size、random_state、可选 units；需 Idempotency-Key，接受后 202 |
| GET /training-runs，GET /training-runs/{id} | 当前权威状态，不执行训练 |
| POST /training-runs/{id}/cancel | 请求取消；RUNNING 须等待进程停止确认 |
| GET /models/{id}，GET /models/{id}/evaluation | 成功模型摘要与完整评估 |
| GET /models/{id}/files/{member} | 仅四个固定 Model Package 成员，必须下载到同一新目录 |

上传仅接受 P1 UTF-8 CSV，限制 20 MiB、100,000 行、256 列、2,000,000 单元格，不截断。
Dataset 单位是权威快照，未知为 null；训练继承并拒绝不一致声明。不自动换算单位。
同认证域/scope/operation/幂等键，同规范化摘要返回原资源当前状态；不同摘要 409，
失败、取消、删除都不因重放而重新执行。文件 bytes 改变即改变上传摘要，即使 CSV 解析结果相同。

下面示例只使用合成数据。先启动 Service 和 Worker，在独立 ML Python 环境执行：

```python
import getpass
import time
from pathlib import Path
from tempfile import TemporaryDirectory
import httpx
import numpy as np
import pandas as pd
from materials_ml import load_package, predict

x = np.arange(60, dtype=float)
table = pd.DataFrame({"x": x, "strength": 2.5 * x + 4})
headers = {"Authorization": "Bearer " + getpass.getpass("Resource token: ")}
base = "/api/v1/scopes/local-example"
with httpx.Client(base_url="http://127.0.0.1:8200", headers=headers, timeout=30, trust_env=False) as client:
    response = client.post(base + "/datasets", headers={"Idempotency-Key": "example-dataset-v1"},
        files={"file": ("synthetic.csv", table.to_csv(index=False).encode(), "text/csv")})
    response.raise_for_status()
    dataset = response.json()
    response = client.post(base + "/training-runs", headers={"Idempotency-Key": "example-training-v1"},
        json={"dataset_id": dataset["id"], "features": ["x"], "target": "strength", "algorithm": "LR"})
    response.raise_for_status()
    run = response.json()
    deadline = time.monotonic() + 1800
    while run["status"] in ("PENDING", "RUNNING"):
        if time.monotonic() > deadline:
            raise TimeoutError("Query timed out; inspect the existing TrainingRun instead of resubmitting.")
        time.sleep(1)
        response = client.get(base + "/training-runs/" + run["id"])
        response.raise_for_status()
        run = response.json()
    assert run["status"] == "SUCCEEDED", run["status"]
    with TemporaryDirectory() as folder:
        for member in ("manifest.json", "pipeline.joblib", "evaluation.json", "splits.json"):
            response = client.get(base + "/models/" + run["model_id"] + "/files/" + member)
            response.raise_for_status()
            (Path(folder) / member).write_bytes(response.content)
        model = load_package(folder, trusted=True)  # This local Service publishes only trusted Worker packages.
        np.testing.assert_allclose(predict(model, table[["x"]]).values, table["strength"],
                                   rtol=1e-10, atol=1e-12)
```

## 独立服务真实验收

安装 service/worker/mcp/dev extras 后，在 Windows 的独立 ML Python 3.11 环境运行：

```powershell
& ./services/materials_ml/scripts/acceptance.ps1
```

入口运行 pip check、全部 P1、共享引用、Service、Worker、Prediction 与真实 MCP Client 测试。
真实集成测试显式读取仓库根 .env 的 PostgreSQL/MinIO 管理配置，为每次运行创建随机独立数据库、
角色、bucket、IAM 身份和合成数据，结束时仅清理这些精确身份；不清空共享卷、不启动平台/LLM/GPU。
它会在临时端口启动真正的 HTTP Service 和 Worker 进程并结束它们，不表示 ML 常驻服务已经启动。

验收包含迁移/唯一约束、认证与跨 scope 拒绝、严格幂等、上传结果未知/数据库发布失败/迟到清理、
LR/RF 下载预测往返、启动门禁/Job 关联失败、取消/Worker 强制退出/Service 重启、旧 claim、
丢失领取/启动/完成回包、部分产物与成功发布事务回滚。另覆盖 Prediction 原子发布、单位/输入上限、进程超时/取消/TCP 断连/Service 强制退出、
同 MCP session 并发跨 scope、三类认证及 REST/MCP 幂等重放。任何真实闭环失败都不能宣称 P3 完成。
直接运行 pytest 而未设 ML_INTEGRATION=1 会跳过真实存储用例，不能替代此入口。

## Prediction 与 MCP

旧数据库须执行 `python -m materials_ml_service.admin migrate` 升级到当前 head（现为 `0003_scopes`）；
`0002_predictions` 仍保留为 Prediction 的增量迁移。不得重新 provision、修改历史迁移或清空既有模型。
Service 以本地互斥锁限制为单实例。

Prediction API 均在 `/api/v1/scopes/{scope_id}` 下：

| 方法与路径 | 合同 |
|---|---|
| POST /predictions | `{model_id,input_dataset_id}`，要求 Idempotency-Key；同步执行，同键重放立即返回原状态 |
| GET /predictions、GET /predictions/{id} | 列表/详情，包括状态、血缘、单位核验和结果引用 |
| GET /predictions/{id}/content | 仅成功结果可下载，固定 predictions.json 合同 |
| POST /predictions/{id}/cancel | 记录取消，进程树停止确认后 CANCELLED；已成功保持成功 |

输入是同 scope 的 AVAILABLE ModelAsset 和 DatasetAsset；必须恰含模型特征，可重排。
上限为原始 CSV 2 MiB、1,000 行、100,000 单元格。已知模型单位要求输入显式完全一致；
未知模型单位始终标记 UNIT_UNVERIFIED，即使输入声明了单位也不能补造训练单位。

Prediction 为 PENDING → RUNNING → SUCCEEDED/FAILED/CANCELLED，PENDING 也可失败或取消。
PENDING 仅是持久化意图；Job Object 纳管门禁子进程、RUNNING 提交确认之后才放行计算。
子进程从创建起最长 30 秒，超时为 PREDICTION_TIMEOUT；存储有独立超时。并发 1，忙时拒绝而不排队。
结果验证和发布也属于 RUNNING；确认进程树停止后，Artifact AVAILABLE 与 Prediction SUCCEEDED 原子提交。
不确定上传/发布沿原身份对账，不重新预测；启动恢复先确认旧进程树停止。

MCP 默认关闭；安装 mcp extra 并配置第三个凭据后，在同一 Service 的 `/mcp` 提供
Streamable HTTP JSON 响应。固定 SDK 1.30.0、sse-starlette 3.0.3，验收协议 2025-11-25。
SDK session 仅承载协议，重启后重新 initialize；没有 MCP Tasks 或领域任务续传。

| Tool | 输入 |
|---|---|
| analyze_tabular_dataset | dataset_id |
| train_tabular_regression | 与 Resource TrainingRequest 相同，返回持久化回执，不等待 Worker |
| get_training_run | training_run_id |
| predict_with_model | model_id、input_dataset_id |

MCP 请求用 MCP token，逐次通过 X-ML-Scope-ID 注入可信 scope；Tool 参数不接受 scope。
一个 session 可服务多个 scope；取消通知须携带目标调用的 scope。同一调用必须使用不可变请求上下文，
并发客户端不得修改共享 HTTP headers 来传递 scope。独立示例为单调用客户端：

```powershell
# 在仅提供 ML_MCP_TOKEN 的客户端终端执行；替换资源 ID。
& ./services/materials_ml/.venv/Scripts/python.exe services/materials_ml/examples/mcp_client.py get_training_run --scope local-example --arguments '{"training_run_id":"RESOURCE_ID"}'
```

训练/预测还要求 `_meta["materials-ml/idempotency-key"]`；示例脚本通过 `--key` 设置。
凭据 audience 与业务幂等空间分离：单用户 Resource/MCP 共享现有 resource 操作空间，凭据不能互换。
同 scope/operation/key、同摘要跨 REST/MCP 返回原资源；不同摘要冲突，终态重放不重新执行。

预测所属 POST 断连或 MCP 取消触发 Prediction 取消并终止进程树；其他连接和同键查询的结束不取消原执行。
已持久化 TrainingRun 不随取消、断连或 session 结束而取消。客户端恢复先重放原键或查询原资源。
Tool 结果包含合同版本、结构化资源摘要和受控错误；请求与结构化结果各限 64 KiB。
完整 CSV、模型包、详细评估和预测值走 Resource API，不作为 Tool 正文发送。

平台可选 MCP Executor 通过统一 Registry/Invocation 调用这些能力；另有独立开关的 ResourceRef、上传代理和删除协调。
可信自然语言资源上下文可独立启用；平台聊天 UI 提供统一附件、消息结果卡片和只读 Viewer，但没有独立
资源中心、训练配置页或 ML 管理后台。启用及隔离环境说明见根 README。

### 平台调用合同 v2

Server 身份为 `materials-ml` / `1.1.0`，Tool 合同为 `materials-ml-tools-v2`。
`predict_with_model` 只正常返回终态；FAILED/CANCELLED 返回 `isError=true` 和原 Prediction。
同键执行中或发布对账中的重放只读等待原任务，默认整次 Tool 调用最多 45 秒。
等待超限返回 `PREDICTION_OUTCOME_UNKNOWN`，不能将 PENDING/RUNNING 当作同步成功，重放者也不获得取消权。

Resource 认证下增加两个只读 POST 接口，均位于 `/api/v1/scopes/{scope_id}`：

- `/operation-identities/prepare` 接收 `operation` 与 `arguments`，返回权威 `request_digest`、`digest_version`、operation 和 scope。
  不预留资源、不运行 fit/predict；平台持久化该身份后，将 `materials-ml/request-digest` 与幂等键放入 MCP `_meta`。
  正式提交在任何资源写入前核对摘要；旧非平台客户端可以省略 expected digest。
- `/operation-receipts/lookup` 只接收 `operation`、`idempotency_key`、`request_digest`、`digest_version`。
  只比较持久化原操作，不读取数据或调用当前 normalizer。NOT_FOUND 仅表示当前未找到，不能据此重派发。

业务 operation 为 `training.submit` / `prediction.submit`，保留原 `training-submit-v1` /
`prediction-request-v1` 摘要规则与 Resource/MCP 共享幂等空间。模型及原始文件仍通过 Resource API 下载。
`scripts/acceptance-p4.ps1 -BackendPython <Backend Python 路径>` 先运行完整 Backend 回归，
再启用真实独立 Backend/ML/Worker 验收；普通 `acceptance.ps1` 不要求 Backend 可选环境。

### 受控资源身份与 scope 关闭

先使用本 Service 的迁移入口升级至 0003。Backend 需要自身 0018，不能连接 ML 数据库执行迁移。
Resource API 新增只读 `resource-identities/{resource_type}/{resource_id}`，返回身份合同版本、远端摘要及固定文件描述；
可变状态不进入身份摘要，身份相同不等于资源当前可用。

`dataset-upload-identities/prepare` 接受与 Dataset 上传相同的 multipart 请求，不创建资源。
平台正式上传携带原 `X-ML-Expected-Request-Digest`；历史 operation receipt 支持 `dataset.upload`，
核查仅比较原 key/digest，不解析当前文件或重算历史身份。独立客户端不携带 expected digest 的原上传方式保留。

`POST /api/v1/scopes/{scope_id}/close-operations` 接收 `operation_id`；GET 同路径 `/{operation_id}` 查询原结果。
活动训练、预测、未确认进程停止及 PENDING 上传返回 BUSY，不自动取消。CLOSED 永久拒绝新工作与普通资源访问，
并将对象清理交给现有维护循环。关闭记录、历史 receipt 和必要清理墓碑保留；CLOSED 不是物理清理完成承诺。
三类 credential role 不变，这些接口仅接受 Resource credential。

P5 完整验收：`services/materials_ml/scripts/acceptance-p5.ps1 -BackendPython <独立 Backend Python>`。
使用临时数据库/bucket、合成数据和两个独立 Python 环境；包含 P1–P4 回归、真实资源代理、持久化 fence 及故障恢复。

P6 基础验收入口为 `services/materials_ml/scripts/acceptance-p6.ps1 -BackendPython <独立 Backend Python>`；
追加 `-RealLLM` 运行独立真实 Provider 语言闭环。默认测试不依赖外部 Provider；未通过真实语言验收不能宣称完整 P6 通过。
