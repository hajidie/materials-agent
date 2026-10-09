# 材料研究智能体（Materials Agent）

面向材料研究的本地聊天式智能体。用户用自然语言描述任务，可调用材料单位换算、ZTA35G 虚拟实验，上传单张 EBSD 图片预测 Inconel 625 屈服强度，或批量分割 TC4 初生 α 相图片。系统保存对话、工具来源、结果附件和最终回答。这里描述代码提供的能力；当前机器上的服务是否已运行，需要按下文现场检查。

## 当前能力与边界

- 聊天界面支持对话、附件、补充条件、停止、结果查看和回答重新生成。模型通过原生工具调用连续处理任务；缺少必要条件时在原对话提问。回答与研究过程通过 SSE 流式展示，保存的过程可在历史对话中展开查看。
- 单位换算、ZTA35G SEM 虚拟实验和 EBSD 屈服强度预测可通过聊天调用。真实推理依赖外部权重与独立 Runtime；Mock 模式只验证协议和交互，不能证明预测准确性。
- TC4 初生 α 相分割：一次聊天提交最多 10 张普通 8 位 PNG/JPEG（灰度或 RGB，兼容完全不透明的 RGBA PNG，单帧，每张 ≤10 MiB，宽高分别 128–4096），一次工具调用逐张处理，输出原尺寸叠加图、黑白二值掩膜和预测面积占比。RGBA 原文件保持不变，推理时转换为 RGB；含透明或半透明像素的图片需先导出为无透明区域的图片。整图包含文件中的细边框；不支持 TIFF、裁边、底栏识别或增强。CSV 仍为单附件，不能与图片混合。下述完整启动命令默认开启。
- Materials ML Service 提供 CSV 资源、LR/RF 训练、预测及 MCP 接入；完整启动命令同时开启这些功能和可信资源上下文，安装与配置见 [ML 服务说明](services/materials_ml/README.md)。
- 消息受理后由 Backend 独立执行，刷新或断网不取消任务。临时模型故障有限重试；重试耗尽、配置待修正或后端重启时暂停，由用户继续。工具结果未知时先核查原操作，不重复派发；达到原任务预算上限后不能继续。Markdown 随流展示，公式在所属消息接收完成后排版。
- 项目面向单用户本地运行，没有登录、多租户或生产部署配置。任务托管和 SSE 广播依赖单个 Backend 进程，不支持直接使用多个独立 Uvicorn worker。

## 仓库导航

| 路径 | 内容 |
| --- | --- |
| `frontend/` | Vue 3 + Vite 界面与 Vitest 测试 |
| `backend/` | FastAPI、Agent Runtime、PostgreSQL/MinIO 适配器、Alembic 迁移与 pytest 测试 |
| `mock-runtime/`、`zta35g-runtime/` | 模拟工具服务与隔离的真实模型服务 |
| `services/materials_ml/`、`packages/materials_storage/` | 独立 ML 服务与对象存储引用合同 |
| `scripts/dev/`、`scripts/acceptance/` | 本地启动与验收脚本 |

业务流程、流式协议和关键边界见 [架构说明](docs/architecture.md)；测试入口、验收场景和证据边界见 [验证指南](docs/testing.md)。

## 本地启动

需要 Windows PowerShell、Docker Desktop、Conda、Node.js 24.14.x 和 npm 11.9.x。首次准备 Backend 与 Mock Runtime 环境、前端依赖；版本以各项目清单为准：

```powershell
conda env create -f environments/materialsagent-backend.yml
conda run -n materialsagent-backend python -m pip install -e './backend[dev,mcp]' -e './mock-runtime[dev]'
npm --prefix frontend ci
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在忽略的根 `.env` 中填写 PostgreSQL、MinIO 等本机配置；根据本机情况替换示例值。Compose 镜像需已在本机，启动脚本不会自动拉取镜像或安装依赖。`Start` 会升级所配置的 Backend 数据库迁移、初始化 SDK checkpoint 表并检查对象存储桶。

日常使用的完整启动命令无需参数，默认使用真实 Provider、Python 3.8 模型 Runtime，并开启全部已实现业务工具：单位换算、SEM、EBSD、TC4 分割、CSV 分析、LR/RF 训练和预测，以及 ML 资源上传、查看与可信资源上下文。

```powershell
.\scripts\dev\start-all.ps1
```

首次配置使用 `conda env create -f environments/materialsagent-zta35g.yml` 创建 Python 3.8 Runtime 环境；还需只读 `SEM/ZTA35G_lab`、外部 EBSD 权重、TC4 权重、Provider 密钥，以及已配置并 provision 的独立 ML Service 环境与存储。根 `.env` 中一次填写 `EBSD_MODEL_ROOT`、`TC4_SEGMENTATION_WEIGHTS` 和 `TC4_RUNTIME_DATA_DIR`，以后不用重复传路径。TC4 回执目录必须持久保存；示例：

```dotenv
EBSD_MODEL_ROOT=C:\本机模型目录\EBSD性能预测
TC4_SEGMENTATION_WEIGHTS=C:\本机模型目录\(epoch150-252)best_epoch_weights.pth
TC4_RUNTIME_DATA_DIR=C:\本机运行数据\tc4-receipts
```

完整启动覆盖 TC4 与三个 ML 功能开关为开启，要求 ML Service 的 MCP 开启且凭据与 Backend 匹配。缺失配置、任一工具不是 `AVAILABLE` 或 ML 资源能力未开启都会启动失败；不会自动跳过或降级为 Mock。故障注入假工具和开发调试路由不属于业务功能，保持关闭。底层仍复用 `local-dev.ps1` 的进程管理、迁移、就绪检查和清理。已有旧启动栈时先执行停止，再使用新命令：

```powershell
.\scripts\dev\start-all.ps1 Stop
.\scripts\dev\start-all.ps1
```

开发或测试时仍可使用原脚本选择功能与 Mock；其无参数默认是 Mock LLM 和 Mock Runtime：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/local-dev.ps1 -Action Start
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready
Invoke-RestMethod http://127.0.0.1:8000/api/v1/tools
```

浏览器打开 `http://127.0.0.1:3000`。`/health/ready` 检查数据库与对象存储；工具可用性应查看 `/api/v1/tools` 中各启用工具的 `availability`。停止本地栈：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/local-dev.ps1 -Action Stop
```

真实模式需要单独的 `materialsagent-zta35g` Python 3.8 环境、只读 `SEM/ZTA35G_lab`、包含 `model/save/CNN_1.pt` 的外部 EBSD 目录，以及所选 Provider 的密钥。先检查 `environments/materialsagent-zta35g.yml`、`backend/config/llm.toml` 和 `.env.example`，再运行：

```powershell
.\scripts\dev\local-dev.ps1 -Action Start -Runtime Real -Llm Provider
```

ML 功能启用时还需独立的 `services/materials_ml/.venv`、配置及存储资源；不要把其依赖安装进 Backend 或真实模型 Runtime。不要提交 `.env`、模型权重或本地运行产物。

完整启动命令默认开启 TC4；底层 `local-dev.ps1` 的按需模式及独立 Backend 配置仍默认关闭。Python 3.8 环境需安装清单中的 `opencv-python-headless==4.10.0.84`；若只想在原有模式上单独加入 TC4，也可运行：

```powershell
.\scripts\dev\local-dev.ps1 -Action Start -Runtime Real -Llm Provider -EnableTc4Segmentation `
  -Tc4Weights 'C:\本机模型目录\(epoch150-252)best_epoch_weights.pth' `
  -Tc4RuntimeDataDir 'C:\本机运行数据\tc4-receipts'
```

独立启动时，Backend 设置 `ENABLE_TC4_SEGMENTATION=true`；Python 3.8 Runtime 设置 `TC4_SEGMENTATION_WEIGHTS` 与 `TC4_RUNTIME_DATA_DIR`。Backend 必须升级至 Alembic head；启用后启动检查要求工具为 `AVAILABLE`。权重 SHA-256 固定为 `d8cfdde273432bef29e3c5e7cd01f857d8e8a6222c57b167f6c198fb14290c7b`，不支持静默替换模型。Runtime 数据目录保存原操作回执、成果与防重放删除标记，应与数据库一起保留，不能在未决操作存在时清空。

单张输入失败继续其他图片；Runtime/GPU 故障或结果未知暂停后续派发。用户点击继续时，先核查原身份，成功项不会重复推理；停止后保留已派发项的回执和成果。对话删除清理所属成果及终态 Runtime 回执；未决计算阻止删除，失败的清理任务由持久队列保留。

## 测试

Backend 全量测试含真实 PostgreSQL/MinIO 用例；前端测试使用 jsdom。直接受影响的测试可先单独运行，再按 [验证指南](docs/testing.md) 扩大范围。Mock、真实 Provider、真实 GPU 和浏览器检查各自提供不同证据，不能互相代替。
