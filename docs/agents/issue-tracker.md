# Issue tracker: GitHub

本仓库的需求、缺陷、规格和任务记录在 GitHub Issues：
https://github.com/hajidie/materials-agent/issues

使用 `gh` CLI 操作。在本仓库内运行时从 Git remote 识别仓库；在其他目录运行时显式加上 `--repo hajidie/materials-agent`。

## 常用操作

- 创建：`gh issue create --title "..." --body-file <正文文件路径>`。
- 读取：`gh issue view <编号> --json number,title,body,labels,comments`。
- 列表：`gh issue list --state open --json number,title,body,labels,comments`；按需加 `--label` 或调整 `--state`。
- 修改正文：`gh issue edit <编号> --body-file <正文文件路径>`。
- 评论：`gh issue comment <编号> --body-file <评论文件路径>`。
- 添加或移除标签：`gh issue edit <编号> --add-label "..."` / `--remove-label "..."`。
- 关闭：`gh issue close <编号>`。

多行正文和评论先保存为 UTF-8 临时文件，再用 `--body-file` 提交，保留真实换行；临时文件不提交到仓库。

## 技能指令的含义

- “publish to the issue tracker”：创建 GitHub Issue。
- “fetch the relevant ticket”：读取指定编号或链接的 Issue。

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub 的 Issue 与 PR 共用编号；遇到类型不明的编号，先用 `gh pr view <编号>` 判断，再读取对应的 Issue 或 PR。

## Wayfinding operations

使用 wayfinder 时遵循：

- Map：一条带 `wayfinder:map` 标签的总 Issue，记录 Notes、Decisions-so-far 和 Fog。
- 子任务：一条独立 Issue；用 `gh issue create --parent <总任务编号> ...` 或 `gh issue edit <总任务编号> --add-sub-issue <子任务编号>` 关联。标签为 `wayfinder:<type>`，type 为 research、prototype、grilling 或 task。
- 无法使用子任务关联时，在总 Issue 正文的任务清单中列出子任务，并在子任务正文顶部写 `Part of #<总任务编号>`。
- 阻塞关系：用 `gh issue edit <任务编号> --add-blocked-by <阻塞任务编号>` 记录；不可用时在正文顶部写 `Blocked by: #<编号>, #<编号>`。所有阻塞任务关闭后才可开始。
- 领取：按总 Issue 中的顺序选择尚未关闭、没有未关闭阻塞项且无人负责的子任务，先运行 `gh issue edit <编号> --add-assignee "@me"`。
- 解决：评论记录结果并关闭子任务，再将结果摘要和链接补充到总 Issue 的 Decisions-so-far。
