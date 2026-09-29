# 材料研究智能体（Materials Agent）

面向材料研究的本地聊天式智能体。用户用自然语言描述任务，可调用材料单位换算、ZTA35G 虚拟实验，或上传单张 EBSD 图片预测 Inconel 625 屈服强度。系统保存对话、工具来源、结果附件和最终回答。这里描述代码提供的能力；当前机器上的服务是否已运行，需要按下文现场检查。

## 当前能力与边界

- 聊天界面支持对话、附件、补充条件、停止、结果查看和回答重新生成。模型通过原生工具调用连续处理任务；缺少必要条件时在原对话提问。
- 默认工具包括单位换算、ZTA35G SEM 虚拟实验和 EBSD 屈服强度预测。真实推理依赖外部权重与独立 Runtime；Mock 模式只验证协议和交互，不能证明预测准确性。
- 可选 Materials ML Service 提供 CSV 资源、LR/RF 训练、预测及 MCP 接入；相关功能默认关闭，安装与运行见 [ML 服务说明](services/materials_ml/README.md)。
- 项目面向单用户本地运行，没有登录、多租户或生产部署配置。当前聊天响应为完整 HTTP 响应，没有逐字流式输出。

## 仓库导航

| 路径 | 内容 |
| --- | --- |
| `frontend/` | Vue 3 + Vite 界面与 Vitest 测试 |
| `backend/` | FastAPI、Agent Runtime、PostgreSQL/MinIO 适配器、Alembic 迁移与 pytest 测试 |
| `mock-runtime/`、`zta35g-runtime/` | 模拟工具服务与隔离的真实模型服务 |
| `services/materials_ml/`、`packages/materials_storage/` | 独立 ML 服务与对象存储引用合同 |
| `scripts/dev/`、`scripts/acceptance/` | 本地启动与验收脚本 |

业务流程和关键边界见 [架构说明](docs/architecture.md)；按改动选择检查项见 [验证指南](docs/testing.md)。

## 本地启动

需要 Windows PowerShell、Docker Desktop、Conda、Node.js 24.14.x 和 npm 11.9.x。首次准备 Backend 与 Mock Runtime 环境、前端依赖；版本以各项目清单为准：

```powershell
conda env create -f environments/materialsagent-backend.yml
conda run -n materialsagent-backend python -m pip install -e './backend[dev,mcp]' -e './mock-runtime[dev]'
npm --prefix frontend ci
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在忽略的根 `.env` 中填写 PostgreSQL、MinIO 等本机配置；根据本机情况替换示例值。Compose 镜像需已在本机，启动脚本不会自动拉取镜像或安装依赖。默认使用 Mock LLM 和 Mock Runtime：

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

## 测试

Backend 全量测试含真实 PostgreSQL/MinIO 用例；前端测试使用 jsdom。直接受影响的测试可先单独运行，再按 [验证指南](docs/testing.md) 扩大范围。Mock、真实 Provider、真实 GPU 和浏览器检查各自提供不同证据，不能互相代替。
