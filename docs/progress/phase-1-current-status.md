# 阶段 1 当前执行状态

> 本文件只保存当前状态、最近一次工作单元摘要和下一步。完整历史由未来经项目负责人验收后的 Git commit 保存。

## 当前状态

| 字段 | 当前值 |
|---|---|
| 当前阶段 | 阶段 1A |
| 当前里程碑 | M4：Mock CHAT_ORCHESTRATION |
| 当前工作单元 | M4-B |
| 状态 | `READY_FOR_M4B` |
| 上一已完成工作单元 | M4-A |
| 当前 branch | `main` |
| 已确认设计基线 | 第一节、第二节、第三节、第四节 A、第四节 B，共五份；本轮未修改 |
| 阶段 1 实施计划 | `docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md`；本轮未修改 |
| 是否处于项目负责人暂停点 | 否 |
| 更新时间 | `2026-07-19T19:04:21+08:00` |

## 最近完成：M4-A 已通过项目负责人验收并提交

M4-A 已在限定 scope 内完成并通过项目负责人最终代码验收。当前状态为 `READY_FOR_M4B`；M4-B 尚未开始，M4 整体尚未完成。

M4-A 当前交付：

- 强类型 `ChatOrchestrationPort`与知识回答、Tool 执行、NEEDS_INPUT 三类候选；
- 确定性 Mock Adapter，仅进行结构化输出解码与安全错误映射；
- ZTA35G 原始输入保留、单位归一化、范围/精度/缺失/歧义校验；
- `LLMCall` Domain、PostgreSQL Repository 与 UnitOfWork 边界；
- Alembic `0004_llm_call` 表、JSONB、状态/时间 CHECK 与现有 Message/TaskInputRevision 外键；
- 安全、有界、深度不可变的 LLM JSON 元数据。

风险主审定向修复：

- LLMCall generation parameters、usage 和 route summary 改为明确受控 Schema，不再接受任意 dict；
- LLMCall JSON 使用精确内建标量、深度不可变映射，Repository 仅在 JSONB 边界转回标准容器；
- `safe_error_message` 限制为最多 256 个可打印单行字符；Mock 意外 responder/decode 异常映射为固定安全错误；
- `ZTA35GValidationResult.to_revision_payloads()` 生成唯一确定性 JSONB-ready 投影，保留 raw 顺序/重复且在投影边界转换 Decimal；
- 候选契约拒绝 object、bytes、Mapping、Decimal 原始候选、非有限/过大数字及标量子类；AmbiguousValue 确定性去重且至少两个不同候选；
- 校验错误码已与设计基线统一；未增加 M4-B 翻译层。

最终验证：

- 完整回归 `329 passed, 0 failed, 0 skipped`；
- Alembic 为唯一 `0004_llm_call` single head，check clean；
- `SCOPE_OK M4A`；
- `SEM_INTEGRITY_OK`；
- 主数据库 actor=1，其余业务表均为 0；
- PostgreSQL/MinIO 容器已停止，named volume 保留。

最终代码审查：`Critical: 0`、`Important: 0`、`Minor: 2`，Minor 均不阻塞 M4-A 验收。

## 已知风险

- generation parameters/usage 白名单仅覆盖 M4 Mock 所需字段，真实 Provider 扩展留到 M12；
- 极端超大 temperature 整数可能产生 `OverflowError`，但该字段是受控配置，当前固定为 0。

## 下一步

当前已具备开始独立 M4-B 工作单元的前置状态；本次仅提交 M4-A，M4-B 尚未开始。
