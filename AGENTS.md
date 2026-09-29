# 仓库协作指南

本文件只放每次修改都可能用到的仓库约定。项目用途与启动见 [README](README.md)；涉及 Agent、工具或数据边界时，再读 [架构说明](docs/architecture.md)；测试入口与证据边界见 [验证指南](docs/testing.md)。按任务读取相关章节，不必在每次小改动前通读所有文档。

## 项目定位

Materials Agent 是本地单用户的材料研究聊天助手。`frontend/` 是 Vue 3 界面，`backend/` 是 FastAPI 应用；`mock-runtime/`、`zta35g-runtime/` 和 `services/materials_ml/` 各有独立职责。以当前代码、配置、迁移和测试为实现依据；旧文档与 Git 历史只用于追溯。

## 修改时遵守

- 先检查目标文件和 Git 状态，保留已有改动。只改本次需求涉及的文件；公共 API、工具合同或持久化结构变化时，同步修改调用方、迁移和相关测试。
- 自然语言意图、工具选择与缺参判断交给模型；Backend 负责参数、资源身份、权限、状态和执行约束的确定性校验。不要在生产路径加入关键词路由或替模型猜科学参数。
- 工具执行经统一 Registry 和 Agent Runtime；真实 ZTA35G/EBSD 模型只在 Python 3.8 Runtime 中加载。Backend 与独立 Materials ML 环境均使用 Python 3.11，依赖不可混装。
- `SEM/` 和外部 EBSD 权重是只读模型资料。不要提交 `.env`、密钥、权重、用户数据或 `tmp/` 产物；不要清空共享 Docker 卷。
- 前端以聊天为主入口。用户通过消息提交任务、补充条件并查看结果；内部 ID、原始异常和工具私有数据不直接进入普通界面。

## 常用验证

在正确的 Python 环境中运行受影响的 pytest 文件；Backend 与 Mock Runtime 的测试根目录分别运行。前端改动运行相关 Vitest，涉及公共类型或打包时运行 `npm --prefix frontend run typecheck` 和 `npm --prefix frontend run build`。跨模块或核心执行链路变更再扩大到相应回归与验收。完成前检查 `git diff --check`，并说明未覆盖的真实 Provider、数据库、浏览器或 GPU 场景。

提交时只暂存已审阅路径；提交说明概括实际改动。
