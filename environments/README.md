# 环境职责

当前 M0 不创建 Conda 环境、不安装 Python 或 Node.js 依赖，也不验证任何候选版本。下列内容只锁定未来环境职责。

| 环境 | 未来职责 | 当前状态 |
|---|---|---|
| `materialsagent-backend` | 模块化单体 Backend、FastAPI、LangChain Adapter、Pydantic、SQLAlchemy、PostgreSQL driver、MinIO SDK 和平台测试 | M1 才开始创建 |
| Node.js / Vue 前端环境 | Vue 3、Vite、TypeScript、前端测试与构建；版本和 lockfile 在前端里程碑固定 | M10 才实现前端 |
| `materialsagent-zta35g` | Python 3.8 旧模型依赖、DDPM、DenseNet、SVR 和真实 ZTA35G Runtime | M13 开始验证 |

## Backend 依赖边界

- 未来 `backend/pyproject.toml` 是 Backend Python 包依赖和版本约束的唯一事实来源。
- 未来 Backend Conda YAML 只负责环境名、Python、pip 和确需 Conda 管理的少量系统级依赖，不重复维护 FastAPI、Pydantic、SQLAlchemy、LangChain 等包版本。
- 旧模型的 PyTorch、Torchvision、CUDA、NumPy、Joblib、scikit-learn 等依赖不得安装进 Backend 环境。
- Backend 不自动创建、启动或重启真实模型环境和 Runtime。

## 真实模型环境边界

- `materialsagent-zta35g` 与 Backend 隔离，真实模型环境从 M13 才开始建立和权重加载验证。
- Python 3.8、PyTorch 1.13.1+cu116、Torchvision 0.14.1+cu116、NumPy 1.22.3、Joblib 1.4.2、Matplotlib 3.2.2 和 scikit-learn 1.0.2 目前都只是已知原环境或候选验证起点，不是已验证锁定版本。
- M13/M14 必须以实际可运行环境生成精确依赖记录、兼容性证据、加载结果和推理测量；候选版本不得表述为已验证版本。
- `SEM/` 在验证前和验证期间保持只读，真实模型环境不得覆盖权重或重构原研究包。

## 当前禁止项

M0 不创建 `materialsagent-backend.yml`、`materialsagent-zta35g.yml`、`backend/pyproject.toml`、`package.json` 或任何虚拟环境，也不执行安装、模型加载、推理、Compose 或服务启动。
