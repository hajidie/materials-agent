# 仓库协作指南

本文件只放每次修改都可能用到的仓库约定。项目用途与启动见 [README](README.md)；涉及 Agent、工具或数据边界时，再读 [架构说明](docs/architecture.md)；测试入口与证据边界见 [验证指南](docs/testing.md)。按任务读取相关章节，不必在每次小改动前通读所有文档。

## 项目定位

Materials Agent 是本地单用户的材料研究聊天助手。`frontend/` 是 Vue 3 界面，`backend/` 是 FastAPI 应用；`mock-runtime/`、`zta35g-runtime/` 和 `services/materials_ml/` 各有独立职责。以当前代码、配置、迁移和测试为实现依据；旧文档与 Git 历史只用于追溯。

## 架构设计原则

- 新增功能前，先明确要解决的问题、业务约束和失败场景；参考 deepseek-harness、Codex、Claude Code 等成熟产品的公开实践(优先参考deepseek-harness)及主流 SDK 文档，借鉴可验证的设计思路，不推测其内部实现或直接照搬。
- 模型调用、工具循环、暂停续轮等通用机制，优先评估 LangChain、LangGraph 等现成能力。只有在符合本项目的状态与运行边界，且能降低整体实现和维护成本时才引入；比较复用与自定义方案并说明取舍。SDK 不覆盖或不适合的业务规则由本项目明确实现，避免重复造轮子，也避免为使用框架而改造业务。

## 修改时遵守

- 先检查目标文件和 Git 状态，保留已有改动。只改本次需求涉及的文件；公共 API、工具合同或持久化结构变化时，同步修改调用方、迁移和相关测试。
- 文档优先更新已有 README、架构说明和验证指南。新增文档须说明具体用途和依据（用户需求、所用技能的明确要求或独立的长期维护需要）；术语表和 ADR 按需维护。开发计划、单次验收结果和排错经过默认记录在 Issue 或交付回复中，不因一次功能开发或代码修改自动新增长期 Markdown 报告。
- 自然语言意图、工具选择与缺参判断交给模型；Backend 负责参数、资源身份、权限、状态和执行约束的确定性校验。不要在生产路径加入关键词路由或替模型猜科学参数。
- 工具执行经统一 Registry 和 Agent Runtime；真实 ZTA35G/EBSD 模型只在 Python 3.8 Runtime 中加载。Backend 与独立 Materials ML 环境均使用 Python 3.11，依赖不可混装。
- `SEM/` 和外部 EBSD 权重是只读模型资料。不要提交 `.env`、密钥、权重、用户数据或 `tmp/` 产物；不要清空共享 Docker 卷。
- 前端以聊天为主入口。用户通过消息提交任务、补充条件并查看结果；内部 ID、原始异常和工具私有数据不直接进入普通界面。

## 常用验证

在正确的 Python 环境中运行受影响的 pytest 文件；Backend 与 Mock Runtime 的测试根目录分别运行。前端改动运行相关 Vitest，涉及公共类型或打包时运行 `npm --prefix frontend run typecheck` 和 `npm --prefix frontend run build`。跨模块或核心执行链路变更再扩大到相应回归与验收。完成前检查 `git diff --check`，并说明未覆盖的真实 Provider、数据库、浏览器或 GPU 场景。

提交时只暂存已审阅路径；提交说明概括实际改动。

## Agent skills

### Issue tracker

需求、缺陷和任务使用 GitHub Issues（`hajidie/materials-agent`）管理。详见 `docs/agents/issue-tracker.md`。

### Domain docs

采用 single-context：根目录 `GLOSSARY.md` 与 `docs/adr/`。详见 `docs/agents/domain.md`。
