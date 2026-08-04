# Materials Agent MVP

本仓库建设面向材料研究的本地材料智能体 MVP。当前唯一注册的 Tool 是 `zta35g_sem_virtual_lab`，目标是让用户通过自然语言请求生成 ZTA35G SEM 图像，并按需获得力学性能结果和可追溯解释。

## 执行状态入口

当前实际里程碑、工作单元、Git 状态和下一步只以 [阶段 1 当前执行状态](docs/progress/phase-1-current-status.md) 为准。本 README 不重复保存这些会随验收和提交变化的动态事实。

稳定的实施顺序为：

```text
G0：阶段 0 收尾与阶段 1 交接基线
→ M0：仓库准备
→ M1：Backend 最小启动与健康检查
→ 后续里程碑
```

未经项目负责人验收，不得提交当前工作单元或进入下一个里程碑。

## 架构概览

平台主体采用模块化单体。未来的 Backend 保存业务编排和公共 API；PostgreSQL 保存结构化事实；MinIO 保存图片；Vue 3 + Vite 提供最小前端。旧模型通过本机回环地址上的独立 Runtime 隔离：

```text
Node.js / Vue 前端
→ materialsagent-backend（候选 Python 3.11）
→ Local ZTA35G Tool Client Adapter
→ materialsagent-zta35g（Python 3.8，阶段 1B 验证）
```

Backend 与旧模型 Runtime 不共享 Python 依赖。独立 Runtime 只是兼容边界，不把平台改造成通用微服务架构。

## 目录职责

当前仓库和后续里程碑使用以下职责边界；尚不存在的目录不会在 M0 提前创建。

| 路径 | 职责 |
|---|---|
| `docs/superpowers/specs/` | 五份已确认设计基线；未经批准不得修改 |
| `docs/superpowers/plans/` | 已确认的阶段 1 实施计划 |
| `docs/progress/` | 当前执行状态与跨 Codex 对话交接 |
| `docs/acceptance/` | 阶段检查清单和 SEM 完整性基线 |
| `scripts/dev/` | 只读检查与本地开发辅助脚本 |
| `environments/` | 环境职责说明；环境文件在对应里程碑才创建 |
| `backend/` | 未来 Backend 包；M1 才允许创建 |
| `frontend/` | 未来 Vue 3 + Vite 前端；M10 才实现 |
| `mock-runtime/` | 未来 Mock Runtime；阶段 1A 使用 |
| `zta35g-runtime/` | 未来真实模型 Runtime；阶段 1B 使用 |
| `SEM/` | 本地只读外部模型包；由 manifest 校验且不进入 Git |

## 权威文档

设计基线：

- [第一节：范围与总体架构](docs/superpowers/specs/2026-07-13-sem-mvp-stage-0-architecture-design.md)
- [第二节：数据流、状态与持久化](docs/superpowers/specs/2026-07-15-sem-mvp-stage-0-data-flow-state-persistence-design.md)
- [第三节：核心数据模型](docs/superpowers/specs/2026-07-16-sem-mvp-stage-0-core-data-model-logical-database-design.md)
- [第四节 A：公共 HTTP API 与聊天时间线](docs/superpowers/specs/2026-07-16-sem-mvp-stage-0-public-http-api-chat-timeline-contract-design.md)
- [第四节 B：本地 Runtime 通信契约](docs/superpowers/specs/2026-07-16-sem-mvp-stage-0-local-zta35g-tool-runtime-communication-contract-design.md)

执行入口：

- [阶段 1 实施计划](docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md)
- [阶段 1 当前状态](docs/progress/phase-1-current-status.md)
- [阶段 1 验收清单](docs/acceptance/phase-1-checklist.md)
- [环境职责说明](environments/README.md)
- [本地开发模式启动与停止](docs/local-development.md)

## 仓库边界检查

仓库提供以下边界检查；它们不是平台启动命令：

```powershell
powershell -ExecutionPolicy Bypass `
  -File scripts/dev/check-sem-integrity.ps1

powershell -ExecutionPolicy Bypass `
  -File scripts/dev/check-scope.ps1 `
  -Milestone M0
```

`SEM/` 不得修改、移动、删除或运行。完整性检查只枚举文件、校验字节数和 SHA-256，不导入模型代码。

## 配置与交接安全

- `.env.example` 只包含安全占位值；真实 `.env`、Secret、凭据和本地数据不得提交。
- 不提交本地数据库、MinIO 对象、生成输出、模型权重、内部路径或机器专用状态。
- 固定模型参数 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000` 不开放为环境变量。
- 新 Codex 对话先读取根 `AGENTS.md` 和 `docs/progress/phase-1-current-status.md`，再重新检查当前 Git branch、HEAD 和工作区。
