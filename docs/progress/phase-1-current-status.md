# 阶段 1 当前执行状态

> 本文件只保存当前状态、最近一次工作单元摘要和下一步。完整历史由未来经项目负责人验收后的 Git commit 保存。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M4：Mock CHAT_ORCHESTRATION |
| 当前工作单元 | M4 Mock CHAT_ORCHESTRATION |
| 状态 | `READY_FOR_M4` |
| 上一已完成工作单元 | M3-B |
| 当前 branch | `main` |
| 已确认设计基线 | 第一节、第二节、第三节、第四节 A、第四节 B，共五份；本轮未修改 |
| 阶段 1 实施计划 | `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`；本轮未修改 |
| 是否处于项目负责人暂停点 | 否 |
| 更新时间 | `2026-07-19T15:00:25+08:00` |

## 最近完成：M3-B 与 M3 已通过验收

M3-B 已通过项目负责人代码、自动化测试和真实 HTTP 验收；M3 整体已经完成并通过验收。

M3 最终交付：

- 服务端 `ActorContext`；
- Conversation 创建与 Actor 所有权列表；
- UserMessage、PENDING Task 与 Conversation.updated_at 原子提交；
- Task 所有权查询；
- 首条消息确定性标题；
- 安全 preview 和有界 cursor；
- 统一 400/422/404/409/500/503 错误包络；
- foreign 与 missing 资源不可区分；
- 标题 best-effort 后处理；
- import/create_app 无数据库、MinIO、LLM 或 Runtime 副作用。

代码审查修复：

- cursor version 严格要求实际整数 1；
- preview 查询次数最多等于 limit；
- Conversation.updated_at 单调不减。

最终验证：

- 聚焦测试 `50 passed`；
- 完整回归 `185 passed, 0 failed, 0 skipped`；
- Alembic heads/current 均为 `0003_task_time_order`，check 无待生成 migration；
- 主数据库 actor=1，conversation/message/task/task_input_revision 均为 0；
- 临时测试和验收数据库均为 0，测试对象均为 0；
- pip check、SEM、M3 scope 和 git diff 全部通过；
- 容器已停止；两个 volume、Actor、migration 和配置 bucket 均保留。

M4 尚未开始。开始 M4 前必须在新的 Codex 对话中重新读取 `AGENTS.md`、Git 状态、current status、M4 计划、第三节和第四节 A，并建立 M4 的新精确 scope allowlist。

## 已知风险

- M3 的消息提交只形成 PENDING Task，不提供知识回答、Tool、结果、解释或时间线；这些能力不得从当前响应推断。
- 公共幂等与真实重放语义尚未实现；`idempotency_replayed=false` 仅是 M3 固定空投影，正式幂等留 M8。
- 标题后处理按设计为 best effort；独立标题事务失败时 title 可保持 null，但已提交的 UserMessage/Task 不受影响。
- Conversation 列表 cursor 是当前本地 MVP 的有界不透明 JSON 编码；统一时间线的稳定锚点、签名和防篡改 cursor 留 M9。

## 下一步

保持 `READY_FOR_M4`，但 M4 尚未开始。本对话只完成 M3-B 验收提交并停止；M4 必须在新的 Codex 对话中按新 scope 开始。
