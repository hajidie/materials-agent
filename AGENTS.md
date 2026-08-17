# Materials Agent — Agent instructions

## 项目与事实来源

本仓库是面向材料研究的本地材料智能体 MVP。平台主体是模块化单体，真实 ZTA35G 模型通过
本机独立 Runtime 与 Backend 隔离；当前唯一 Tool 是 `zta35g_sem_virtual_lab`。

处理任务时按以下顺序判断事实：

1. 项目负责人当前明确指令；
2. 当前代码、配置、数据库迁移、测试和实际运行行为；
3. 根 `README.md` 中面向使用者的稳定项目说明；
4. Git 历史中的旧设计决定、实施计划和验收记录。

代码、测试、运行行为或 README 彼此冲突时，先查明差异并报告；不得仅为通过测试而改变
产品语义，也不得把历史文档当成现役合同。

## 知识治理

- 不创建动态进度文件、里程碑日志、一次性交接摘要或逐次验收报告；当前分支、HEAD、改动、
  测试结果和下一步必须现场检查。
- `README.md` 只记录稳定的用途、架构、运行方式和限制。实现改变这些稳定事实时同步更新。
- 稳定的架构决策、运维说明或子目录专属规则，只有在 README 无法清晰承载且具有长期价值
  时才新增；不得复制现有事实形成第二套权威说明。
- 历史背景确有必要时使用 `git log -- <path>` 和 `git show <commit>:<path>` 查阅，不把旧
  文档恢复到当前树中充当现役事实来源。

## 安全与架构硬边界

- Backend 使用 Python 3.11；真实 ZTA35G Runtime 使用隔离的 Python 3.8 环境。不得把
  PyTorch、CUDA、Joblib、scikit-learn 等旧模型依赖安装进 Backend 环境。
- `SEM/` 是本地只读外部模型包，不进入普通 Git 跟踪。不得修改、移动、删除或通过整理
  代码重构它；完整性基线是 `docs/acceptance/sem-package-manifest.json`。
- 不把 Tensor、完整 Prompt、Secret、完整 Provider 原始响应、图片 bytes、权重路径、
  内部绝对路径或本地数据写入日志、公共响应或版本库。
- 固定推理参数 `num_samples=1`、`guide_scale=2.0`、`timesteps=1000` 属于当前 Runtime
  合同；变更时必须同步契约、实现和测试，不得仅开放为环境变量。
- 不混合不同 ToolRun 的图片和性能结果。Backend 不自动重试 Runtime execute；显式重试
  必须创建新的 ToolRun 和 seed。
- 外部 LLM、Runtime 和 MinIO 调用不得置于数据库长事务中。

## 当前产品范围

当前版本是单用户、本地运行、单 Tool 的 MVP，不包含 Redis、后台 Worker、SSE、WebSocket、
登录、多 Tool、真实 SEM 上传或生产部署。增加这些能力属于产品或架构范围变更；实施前必须
确认范围，并同步相关实现、迁移、测试和 README。

## 工作方式

- 开始前读取本文件，检查当前 Git branch、HEAD、暂存区和工作区，再读取与任务直接相关
  的代码、配置、迁移和测试；需要运行、维护或解释项目时读取 README 的相关章节。
- Git 实际状态始终优先。工作区可能已有用户改动；不得覆盖、回退或顺带整理无关内容。
- 只修改当前任务需要的文件。遇到跨范围需求或行为语义不清时，先从实现和测试取证；会改变
  产品或架构语义时停止并请项目负责人确认。
- 行为变更必须新增或同步测试；修复缺陷时应在可行范围内增加能够复现原问题的回归测试。
- 外部 Provider、真实 Runtime、GPU、Docker 服务和浏览器流程仅在任务明确需要且安全前提
  满足时运行。

## 最小验证

- 修改 `backend/**`：运行 `python -m pytest backend/tests -q`。
- 修改 `frontend/**`：运行 `npm --prefix frontend test -- --run`、
  `npm --prefix frontend run typecheck` 和 `npm --prefix frontend run build`。
- 修改 `scripts/dev/**`：运行对应的离线测试；涉及本地栈或范围检查时，分别运行
  `& .\scripts\dev\test-local-dev.ps1` 或 `& .\scripts\dev\test-check-scope.ps1`。
- 修改 `mock-runtime/**`：运行 `python -m pytest mock-runtime/tests -q`。
- 修改 `zta35g-runtime/**`：在 `materialsagent-zta35g` Python 3.8 环境中运行相关单元、
  契约或兼容性测试；除非任务明确要求，不加载模型或占用 GPU。
- 修改根配置、环境声明、数据库迁移或验收脚本：运行最接近受影响行为的配置、迁移、契约或
  离线验收门禁；没有确定性门禁时，明确报告人工验证依据和未覆盖风险。
- 仅修改文档或规则：使用精确改动路径运行 `& .\scripts\dev\check-scope.ps1`。
- 所有改动在声明完成前都必须运行 `git diff --check`，检查最终 diff 和 `git status`，并如实
  报告未运行、跳过或依赖外部环境的验证。

## Code Review Rules

- 优先报告行为语义、状态一致性、数据隔离、幂等与重试、安全边界、数据库迁移和公共契约
  问题；格式化和机械风格问题交给仓库现有工具。
- 每项发现应指出具体文件位置、影响和验证依据；没有发现时仍需说明未覆盖的环境或风险。

## Git 与敏感信息

- 未经项目负责人明确授权，不执行暂存、commit、amend、push、merge 或创建 PR。
- 禁止使用 `git add .` 和 `git add -A`；获准暂存时只显式列出已批准路径。
- 不提交真实 `.env`、凭据、数据库、对象存储数据、生成输出、模型权重、缓存、日志、会话、
  临时目录、worktree 或机器专用状态。
- `.codex/` 仅允许经明确审阅、无 Secret、无个人绝对路径、无机器状态的稳定项目配置。
- 不使用破坏性 Git 命令覆盖现有工作区；删除前必须确认精确目标、影响和授权范围。
