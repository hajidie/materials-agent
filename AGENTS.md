# Repository guidance

## 项目定位

本仓库建设面向材料研究的本地材料智能体 MVP。当前只注册一个 `zta35g_sem_virtual_lab` Tool。平台采用模块化单体：Backend 与旧模型 Runtime 使用两个隔离环境，PostgreSQL 保存结构化事实，MinIO 保存图片，Vue 3 + Vite 提供最小前端。首版聊天编排使用 LangChain，不使用 LangGraph。

## 权威文档索引

五份已确认设计基线：

- `docs/superpowers/specs/2026-07-13-sem-mvp-stage-0-architecture-design.md`
- `docs/superpowers/specs/2026-07-15-sem-mvp-stage-0-data-flow-state-persistence-design.md`
- `docs/superpowers/specs/2026-07-16-sem-mvp-stage-0-core-data-model-logical-database-design.md`
- `docs/superpowers/specs/2026-07-16-sem-mvp-stage-0-public-http-api-chat-timeline-contract-design.md`
- `docs/superpowers/specs/2026-07-16-sem-mvp-stage-0-local-zta35g-tool-runtime-communication-contract-design.md`

阶段 1 详细实施计划：

- `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`

产品与架构语义优先级：

```text
当前明确指令
>
已确认设计基线
>
经项目负责人确认的实施计划
>
旧聊天内容
```

执行状态优先级：

```text
当前 Git 实际状态
>
docs/progress/phase-1-current-status.md
>
旧交接摘要
```

计划文件若仍标记“待项目负责人复核”，不得把它表述为已确认计划或据此越过暂停点。

## 永久边界

- 除非项目负责人明确要求设计变更，不修改五份已确认设计基线。
- 阶段 1A 不加载或运行真实模型；`SEM/` 始终保持只读。
- 不把旧模型的 Python、PyTorch、CUDA、Joblib 或 scikit-learn 依赖安装进 Backend 环境。
- 不提前实现 Redis、Worker、SSE、WebSocket、登录、多 Tool、真实 SEM 上传或生产部署。
- 不把 Tensor、完整 Prompt、Secret、完整 provider 原始响应、图片 bytes、权重路径或内部路径写入日志或公共响应。
- 不擅自改变 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000` 等固定运行参数。
- 不把不同 ToolRun 的图片和性能结果混合。
- Backend 不自动重试 Runtime execute；显式重试必须创建新的 ToolRun 和 seed。
- 不使用 `git add .` 或 `git add -A`。

## 工作方式

- 开始前依次读取本文件、当前 Git branch/HEAD/status、`docs/progress/phase-1-current-status.md`、当前里程碑计划、直接相关设计基线、相关代码与测试。
- 一个独立、可验收的工作单元约等于一个 Codex 对话；一个里程碑可以拆成多个连续对话。
- 预计超过约 12 个生产文件、包含两个明显独立闭环或上下文开始混淆时，在同一里程碑内拆分工作单元。
- 遇到跨范围需求或设计冲突时停止修改并报告，不在实现中自行改写语义。
- 使用测试先行或测试同步；外部 LLM、Runtime、MinIO 调用不得放在数据库长事务中。
- 只修改当前任务明确允许的文件范围，不顺带创建后续里程碑内容。
- 任何“完成”声明必须附带本轮新执行的测试和命令证据。
- 每个工作单元结束时运行局部测试和 `git diff --check`，汇报 Git diff，更新当前进度文件，并停在负责人验收点。
- 未经项目负责人验收不得提交、不得进入下一工作单元或下一里程碑。

## 变更记录放置

- 产品语义、架构契约或边界变化：先提出设计变更，经批准后修改 `docs/superpowers/specs/` 中对应基线。
- 已确认范围内的实施步骤、文件范围和验收命令变化：修改阶段 1 计划。
- 当前 branch、HEAD、验证结果、未提交文件、阻塞和下一步：更新 `docs/progress/phase-1-current-status.md`。
- 不在 `AGENTS.md` 保存临时 HEAD、一次性 blocker、某次测试结果或完整历史。

## Git 规则

- 开始前检查当前 branch、HEAD 和工作区；以实际状态为准。
- 不修改无关文件，不覆盖已有未提交内容。
- 不提交 Secret、`.env`、凭据、本地数据库/对象数据、生成输出、模型权重或本地专用状态。
- 每次 commit 只对应一个已由项目负责人验收的工作单元；只有项目负责人明确批准后才执行 commit。
- 只显式暂存批准文件；禁止批量暂存整个仓库。
- `SEM/` 不进入普通 Git 跟踪，通过 `docs/acceptance/sem-package-manifest.json` 和 `scripts/dev/check-sem-integrity.ps1` 检查完整性。
- `.codex/` 只允许明确审阅过、无 Secret、无个人绝对路径、无机器状态的稳定项目配置保持可跟踪；缓存、日志、会话、临时目录和 worktree 不得提交。

## 每轮结束汇报格式

按顺序汇报：

1. 当前 branch、HEAD、暂存区和工作区状态；
2. 实际读取的文件；
3. 修改文件；
4. 完成内容；
5. 实际执行命令；
6. 测试结果；
7. 人工验收路径；
8. Git diff 摘要；
9. 已知风险；
10. 当前进度文件是否更新；
11. 下一步建议；
12. 是否执行 commit。
