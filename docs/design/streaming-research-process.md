# 科研过程流式展示设计

状态：已实现并提交。实现提交为 `74cedf4`，后续审查修复为 `0bf0db5`；运行态与验收范围见 [验收记录](../acceptance/streaming-research-process.md)。

更新：2026-10-08。

## 用户体验与业务边界

- 展示 Provider 返回的模型思考、模型生成的行动说明、真实工具执行状态和公开结果摘要；模型思考不作为科研证据。
- 保存累计过程内容，不保存逐字播放轨迹；历史中可展开查看，失败或中止时已保存的未完成片段继续标为未完成。
- 正文先显示为“正在生成”；同次模型调用后续请求工具时归入过程，正式回答提交后归入回答。结束后默认折叠过程，保留用户主动展开的选择。
- 行动说明由模型提供；没有说明时，根据真实工具状态显示固定进度文案，不增加总结模型。补充问题和工具确认始终在主区域可见。
- 刷新、断网或关闭页面不取消任务。Backend 重启后显示中断，由用户触发继续并先核查原工具回执，不重派结果不明的操作。
- Markdown 随流显示，数学公式在所属消息接收完成后排版；不新增科学结论 Validator，现有参数、权限、预算、输出完整性与私有值保护继续生效。

业务术语见 [GLOSSARY](../../GLOSSARY.md)，保存与执行生命周期的决定见 [ADR-0001](../adr/0001-retain-research-process-content.md)、[ADR-0002](../adr/0002-run-independent-of-browser-connection.md)。

## 模块职责

```text
提交消息 / 回答补充 / 确认 / 重试 / 重新生成
    → 原业务受理与幂等校验
    → AgentRuntime 后台任务托管与业务执行
        → SdkAgentLoop：SDK 模型与工具续轮
        → AgentProcess：公开正文、思考和工具事实
            ├─ PostgreSQL 累计内容快照
            └─ SSE 订阅 → Vue 当前输出 + 可折叠过程
```

| 入口 | 职责 |
| --- | --- |
| `backend/src/materialsagent/application/agent_runtime.py` | 受理后托管后台任务，同一 Run 串行推进；再次调度标记避免确认/补充发生在旧任务收尾时被遗漏。负责状态、预算、权限、工具和正式回答提交。 |
| `backend/src/materialsagent/application/sdk_agent_loop.py` | 复用 LangChain `create_agent` 与经典 `astream` v2，规范化分片、完整消息和暂停信息。 |
| `backend/src/materialsagent/application/agent_process.py` | 跨分片公开文本保护、过程段归并、内容快照和订阅通知。 |
| `backend/src/materialsagent/api/routes/agent_runs.py` | 消息受理、控制动作、`/process` 快照、`/events` SSE 和显式 `/resume`。 |
| `frontend/src/composables/useProcessStreams.ts` | EventSource 生命周期、快照替换、段修订归并和旧连接隔离。 |
| `frontend/src/components/ResearchProcess.vue`、`AssistantMarkdown.vue` | 当前输出、折叠过程及历史 Markdown 的统一展示。 |

业务 `AgentRun` 是状态权威，数据库 version/claim 保证唯一推进者；SDK checkpoint 负责模型续轮，公开过程快照负责历史展示。三者不能互相替代。公共旧 `/advance` 已移除，受理后由 Backend 调度；内部 `advance` 仍是执行方法。

当前实现依赖单个 Backend 进程的任务托管和广播。多个独立 Uvicorn worker 无法共享这些进程内状态，扩展部署前必须重新设计执行与订阅协调。

## SDK 适配与完成语义

`SdkAgentLoop` 消费 `astream(..., stream_mode=["messages", "updates", "values"], version="v2")`。`messages` 提供即时增量，以固定 `agent_call_id` 关联完整响应；`values` 提供完整状态。v2 顶层 `interrupts` 规范化为 Runtime 使用的 `__interrupt__`，避免把 `ask_user` 暂停误判为最终回答。

Provider 模型适配层显式开启 Agent 调用的 `streaming=True` 与 `stream_usage=True`，包括 `before_model` 重建模型的路径。业务回调继续执行完整响应校验、预算核算和工具授权，半截工具参数不会触发执行。工具执行事实来自 Runtime 的业务记录。

这里复用 SDK 的流式与 checkpoint 能力，以 v2 保持现有完整消息合同，避免新增 Provider wire parser。SDK 不承担的权限、资源、预算和持久化规则仍由 Backend 实现。

三个完成时刻必须分开：

1. 所属 Markdown 段接收结束，渲染器可排版公式，不表示任务结束。
2. 模型调用完成，完整消息仍可能请求工具或等待用户。
3. 正式回答和 Run 成功状态一起提交，前端才显示已完成和回答版本操作。

正式回答提交后用保存正文替换实时预览，包括后端追加的单位说明，避免重复追加。重新生成关联新的 Run/回答版本，保留原过程；它只能使用已冻结结果，不调用工具。

## 过程内容与私有值边界

`0025_agent_process_stream` 新增独立 `agent_process` 表，并增加非终态 `INTERRUPTED`。过程记录随所属 Run 级联删除；清理 SDK checkpoint 不删除历史过程。

过程段拥有稳定 `segment_id`、顺序和 revision；内容区分 reasoning、text、tool，状态区分 streaming、complete、interrupted。正文另有 pending、process、answer 展示用途，重新归类不复制可见内容。工具段由已提交的执行和 Observation 公开投影生成，不暴露私有参数与原始结果。完整消息已收口后，迟到增量不能重新打开该段。

有内容更新时约每秒保存一次累计快照；完成、等待、失败和停止时强制收口保存。这不是逐秒定时器，也不是硬性丢失上界：突然断电可能丢失自上次保存以来的内容。

文本与思考分别维护跨分片尾部缓冲，已知私有值完成匹配和脱敏后才进入 SSE 与存储。结束、失败和停止也处理尾部，隐藏可疑未完成引用；完整消息仍经原校验。这是已知私有值保护，不是任意敏感信息识别器。

保存、复制和回答版本使用公开原始 Markdown。remend 临时补齐的显示副本不写回记录，不送回模型；Provider 续轮所需原始 reasoning 保留在 SDK 消息路径。

## SSE 与重连

快照读取和订阅沿用 Actor/Conversation 访问校验；订阅只观察，控制动作使用 POST 业务请求。

| 事件 | 合同 |
| --- | --- |
| `snapshot` | 每次连接先发送完整公开过程和 Run 状态，包括携带旧 Last-Event-ID 的重连。 |
| `process.updated` | 变更段的累计完整内容、revision 和当前公开 Run，不是字符追加指令。 |
| `settled` | 本轮后台任务已收尾，Run 处于等待、终结或中断状态；客户端关闭订阅。 |
| 心跳注释 | 空闲约 10 秒发送一次。 |

不维护 token 事件重放缓冲；事件 id 只表示当前连接传输顺序，重连始终从累计快照恢复。通道有 epoch，段有稳定身份与 revision。先注册订阅再读快照；前端按 epoch 替换、按段 revision 归并，切换对话后忽略旧连接。

每个订阅队列只保留一个唤醒通知，约 40ms 合并更新，慢客户端读取最新累计状态，不阻塞生成者。已保存、无订阅且已收口的通道可淘汰，历史从数据库加载。SSE 响应使用 `text/event-stream`、禁止缓存和代理缓冲；Vite 代理保持长连接超时。

## 中断、恢复与停止

Backend 启动时把旧活动 Run 收敛为中断并保存过程和预算；等待补充或确认的 Run 保留等待语义。checkpoint 缺失且已有模型调用或待执行操作时终止为 `CHECKPOINT_MISSING`，不能伪装成可恢复。

用户继续时校验 submission 身份与 Run 版本，先核查原工具回执。已完成结果可复用；没有可验证回执则保留中断并等待，禁止自动重派。新模型调用保留旧片段的未完成标记。

停止取消后续模型决策，已派发 GPU/ML 工作可能继续，迟到结果仍按原回执保存。存在 `INTERRUPTED` Run 时，迁移拒绝降级；先解决中断状态才能降级。

## Markdown 与界面

使用锁定依赖 remend、markdown-it、`@mdit/plugin-katex` 与 KaTeX，版本以 `frontend/package.json` 和 lockfile 为准。选择该组合是为了流中显示修补和完成态原文解析分离；先前的替代库探针属于选型历史，不是当前集成验收。

`AssistantMarkdown` 只接收公开文本、所属段是否完成及样式变体，实时和历史共用同一组件。未完成时 remend 只修补显示字符串，完成后直接解析原文。数学支持 `$...$`、`$$...$$`、`\(...\)` 和 `\[...\]`，未完成时显示源码。

渲染禁用原始 HTML，限制链接协议并设置外链隔离属性；Markdown 图片只显示替代文字，不请求模型给出的 URL。结果图片仍使用原受控附件，不将 Markdown URL 当作附件授权。没有 Mermaid、自定义 Vue 组件或代码执行。

组件在输入变化时解析完整 Markdown，没有跨调用增量 AST；已完成段的输入不变时复用计算结果，不承诺 O(增量长度) 性能。过程展开选择跟随 Run，等待交互始终可见；用户向上阅读时不强制滚回底部。

## 验证与参考

相关测试入口见 [验证指南](../testing.md#研究过程流式展示)，已有集成、Provider、浏览器及 GPU 证据和未覆盖项统一保存在 [验收记录](../acceptance/streaming-research-process.md)。单元测试或历史验收不能证明本机当前已部署。

- [LangChain 流式文档](https://docs.langchain.com/oss/python/langchain/streaming)
- [LangGraph 流式与状态文档](https://docs.langchain.com/oss/python/langgraph/streaming)
- [DeepSeek 思考与工具续轮说明](https://api-docs.deepseek.com/guides/thinking_mode/)
- [remend 固定源码](https://github.com/vercel/streamdown/tree/08da224d303e1ef2516e2be4ce04d0c55fbd68ea/packages/remend)
- [markdown-it 固定源码](https://github.com/markdown-it/markdown-it/tree/3c51991c32aaa2b002a52c009334ebe5752c84b3)
- [Harness 类型化流与消息结算](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/agent-loop/src/assistant-stream.ts)
- [Harness 过程与答案投影](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/client/ui-chat/src/client/conversation-nodes/turn-process.ts)
