# Repository guidance

## 项目与事实来源

本仓库是面向材料研究的本地材料智能体 MVP。当前只提供
`zta35g_sem_virtual_lab` Tool；平台主体是模块化单体，真实 ZTA35G 模型通过本机独立
Runtime 与 Backend 隔离。

处理任务时按以下顺序判断事实：

1. 项目负责人当前明确指令；
2. 当前代码、配置、数据库迁移和测试所体现的行为；
3. 根 `README.md` 中面向使用者的稳定项目说明；
4. Git 历史中的旧设计决定、实施计划和验收记录。

仓库不维护动态进度文件、里程碑日志、交接摘要或逐次验收报告。当前分支、HEAD、改动、
测试结果和下一步必须现场检查，不写入 Markdown。除非项目负责人明确改变文档治理，仓库
只保留本文件与根 `README.md` 两份 Markdown。

## 永久边界

- Backend 使用 Python 3.11；真实 ZTA35G Runtime 使用隔离的 Python 3.8 环境。不得把
  PyTorch、CUDA、Joblib、scikit-learn 等旧模型依赖安装进 Backend 环境。
- `SEM/` 是本地只读外部模型包，不进入普通 Git 跟踪。不得修改、移动、删除或通过整理
  代码重构它；完整性基线是 `docs/acceptance/sem-package-manifest.json`。
- 未经明确设计变更，不增加 Redis、Worker、SSE、WebSocket、登录、多 Tool、真实 SEM
  上传或生产部署能力。
- 不把 Tensor、完整 Prompt、Secret、完整 Provider 原始响应、图片 bytes、权重路径、
  内部绝对路径或本地数据写入日志、公共响应或版本库。
- 固定推理参数 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000` 不得擅自改变或开放
  为环境变量。
- 不混合不同 ToolRun 的图片和性能结果。Backend 不自动重试 Runtime execute；显式重试
  必须创建新的 ToolRun 和 seed。
- 外部 LLM、Runtime 和 MinIO 调用不得置于数据库长事务中。

## 工作方式

- 开始前读取本文件，检查当前 Git branch、HEAD、暂存区和工作区，再读取与任务直接相关
  的代码、配置、迁移和测试；需要使用或维护项目时再查 `README.md`。
- Git 实际状态始终优先。工作区可能已有用户改动；不得覆盖、回退或顺带整理无关内容。
- 只修改当前任务需要的文件。遇到跨范围需求、行为语义不清或代码与 README 冲突时，先
  从实现和测试取证；会改变产品或架构语义时停止并请项目负责人确认。
- 功能、修复和行为变更使用测试先行或测试同步。外部 Provider、真实 Runtime、GPU、
  Docker 服务和浏览器流程仅在任务明确需要且安全前提满足时运行。
- `README.md` 只记录稳定的用途、架构、运行方式和限制。实现改变这些稳定事实时同步更新；
  不写日期、当前 HEAD、临时 blocker、某次测试结果或下一步计划。
- 历史背景确有必要时使用 `git log -- <path>` 和 `git show <commit>:<path>` 查阅，不把旧
  文档恢复到当前树中充当第二套事实来源。
- 声明完成前至少运行与改动直接相关的测试和 `git diff --check`，并检查最终 Git diff。

## Git 与敏感信息

- 未经项目负责人明确授权，不执行暂存、commit、amend、push、merge 或创建 PR。
- 禁止使用 `git add .` 和 `git add -A`；获准暂存时只显式列出已批准路径。
- 不提交真实 `.env`、凭据、数据库、对象存储数据、生成输出、模型权重、缓存、日志、会话、
  临时目录、worktree 或机器专用状态。
- `.codex/` 仅允许经明确审阅、无 Secret、无个人绝对路径、无机器状态的稳定项目配置。
- 不使用破坏性 Git 命令覆盖现有工作区；物料删除必须先确认精确目标与授权范围。
