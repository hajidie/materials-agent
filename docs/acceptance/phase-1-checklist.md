# 阶段 1 验收清单

本清单用于查看静态里程碑验收结果、正式暂停点和禁止提前实现的边界，不替代已确认的阶段 1 实施计划。

当前实际执行状态只以 [阶段 1 当前执行状态](../progress/phase-1-current-status.md) 为准。本文件不保存里程碑的实时状态。

## 里程碑验收路线

| 里程碑 | 主要验收结果 |
|---|---|
| G0 | 阶段 0 收尾与阶段 1 交接基线 |
| M0 | 仓库规范、配置示例、说明和范围检查 |
| M1 | Backend 最小启动与健康检查 |
| M2 | PostgreSQL、MinIO 与基础持久化 |
| M3 | Conversation、Message、Task 最小闭环 |
| M4 | Mock CHAT_ORCHESTRATION |
| M5 | Mock Runtime、Tool Registry 与 ToolRun |
| M6 | Asset PENDING → AVAILABLE |
| M7 | ToolResult 与 Explanation |
| M8 | 幂等、重试与失败路径 |
| M9 | Conversation 统一时间线 API |
| M10 | 最小 Vue 3 + Vite 前端 |
| M11 | 阶段 1A Mock 端到端验收 |
| M12 | 真实 LangChain 与 LLM Provider 接入 |
| M13 | Python 3.8 模型环境与权重加载验证 |
| M14 | 真实模型最小推理与测量 |
| M15 | 真实 Runtime 与 Local Tool Client Adapter |
| M16 | 真实 LLM 与真实 Runtime 完整端到端验收 |

## 八个正式项目负责人暂停检查点

| 暂停点 | 必须确认的结果 | 未确认时禁止 |
|---|---|---|
| G0 | 根规则、动态进度、忽略策略、SEM manifest/完整性基线 | 开始 M0 |
| M2 | Backend、PostgreSQL、MinIO 和基础持久化可启动 | 进入业务闭环 |
| M8 | Mock Tool 的结果、失败、幂等和重试全链路 | 进入时间线和前端收口 |
| M11 | 统一时间线、最小 Vue 前端和 Mock E2E | 宣布阶段 1A 完成或接真实 LLM |
| M12 | 真实 LLM 三类编排、参数提取、Explanation 和安全失败路径 | 进入真实模型环境 |
| M13 | Python 3.8 环境、全部权重加载和精确兼容性证据 | 运行真实推理 |
| M14 | 真实 SEM/性能结果、固定参数和性能记录可接受 | 实现真实 Runtime 集成 |
| M16 | 真实 LLM 与真实 Runtime 同时启用的完整 E2E | 宣布阶段 1 完成 |

除上述八个正式里程碑门之外，每个工作单元结束也必须停在项目负责人验收点；这不新增第九个正式里程碑门。

## 分阶段禁止提前实现

### M0 仓库准备

- 不创建 Backend、Frontend、Mock Runtime、真实 Runtime、环境、依赖、Compose、数据库或 Migration。
- 不加载、运行或修改 `SEM/`，不调用 LLM，不创建真实 `.env` 或 Secret。
- 不修改五份设计基线、阶段 1 计划或根 `AGENTS.md`，不进入 M1。

### 阶段 1A：M1–M11

- 只使用 Mock LLM 和 Mock Runtime，不加载或运行真实模型。
- 不实现 Redis、Worker、SSE、WebSocket、登录、多 Tool、真实 SEM 上传、GPU 容器或生产部署。
- 不把旧模型依赖装入 Backend，不把固定运行参数开放给用户。
- Mock LLM 与 Mock Runtime 必须保留为自动化测试替身，不得因后续真实接入而删除。

### 真实 LLM：M12

- 只通过 LangChain Adapter 和 Explanation Adapter 接入真实 Provider；LLM 不直接执行 Tool。
- API Key 只来自环境变量；不保存完整 Prompt、Secret 或 Provider 原始响应。
- M12 通过前不进入真实模型环境与权重加载。

### 阶段 1B：M13–M16

- M13 只建立 Python 3.8 隔离环境并验证全部权重加载，不做真实推理。
- M14 才首次执行最小真实推理，必须验证 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000`，不得把它们开放为可调参数。
- M15 才实现真实 Runtime 与 Adapter；Runtime 只绑定 `127.0.0.1`，Backend 不自动重试 execute。
- M16 必须同时使用真实 LLM 和真实 ZTA35G Runtime 完成 Vue → Backend → LLM → Runtime → MinIO → 时间线 E2E；只启用一侧不算通过。

## SEM 与测试替身检查

- 每个涉及仓库边界的工作单元都运行 `scripts/dev/check-sem-integrity.ps1`，预期 `SEM_INTEGRITY_OK`。
- 当前基线必须保持 57 个文件、2,043,071,133 字节，aggregate fingerprint 为 `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`。
- `SEM/` 被 Git 忽略不等于安全未变；scope 检查不能替代 SEM 完整性检查。
- Mock LLM 和 Mock Runtime 在真实能力接入后继续用于自动化测试和失败注入。

## 工作单元完成门

每个工作单元必须同时满足：

- 局部自动化测试与人工验收命令已重新执行并记录退出码；
- `git diff --check` 为零错误，Git diff 只包含允许范围；
- `docs/progress/phase-1-current-status.md` 已更新；
- 项目负责人已验收后，才允许显式暂存批准文件并 commit；
- 未验收时不得暂存、commit 或进入下一工作单元/里程碑。
