# 科研过程流式展示设计

状态：方案已确认，工作区已实现；未提交 Git，未迁移用户业务数据库。

更新：2026-10-08。

## 已确认的体验与范围

| 议题 | 决定 |
| --- | --- |
| Q1 过程内容 | 展示 Provider 返回的模型思考、已有行动说明、真实工具执行状态和公开结果摘要；模型思考不作为科研证据。 |
| Q2 历史回看 | 保存过程内容，包含思考正文，不保存逐字播放轨迹；已保存的失败或中止片段标为未完成。 |
| Q3 页面断开 | 后端继续处理原任务；重新打开可以恢复查看，重连不派发任务。 |
| Q4 正文归类 | 先在主区域显示“正在生成”；同次模型调用后续出现工具调用时归入过程，正式回答提交后保留在正文区。过程结束后默认折叠，尊重用户主动展开的选择。 |
| Q5 行动说明 | 模型写了就展示，没有则根据真实工具状态显示固定进度文案，不增加总结模型或强制每步生成解释。 |
| Q6 后端重启 | 保留过程并显示中断；用户触发恢复后，先核查已派发工具回执，再决定如何继续，不自动重跑结果不明的工具。 |
| Q7 Markdown | 支持标题、列表、表格、代码块、链接和数学公式。普通 Markdown 随流更新，公式在所属消息完成后排版。 |

不新增最终回答 Validator。现有参数、资源、预算、输出完整性和私有值保护仍需保留并适配流式路径。

业务术语见 [GLOSSARY](../../GLOSSARY.md)，历史保存和执行生命周期分别见 [ADR-0001](../adr/0001-retain-research-process-content.md)、[ADR-0002](../adr/0002-run-independent-of-browser-connection.md)。

## 实现入口

- `sdk_agent_loop.py` 使用现有 LangChain `create_agent` 的经典 `astream` v2；模型调用固定 `call_id`，增量消息与完整消息按该身份收口，避免迟到分片重复追加。
- `agent_runtime.py` 在受理成功后托管后台任务；同一个 Run 串行处理确认/补充，使用再次调度标记防止在旧任务收尾时遗漏新请求。
- `agent_process.py` 保存公开思考、正文、活动说明和工具投影，约每秒保存累计内容，完成/停止/等待时强制保存。独立 `agent_process` 表随原 Run 级联删除。
- `agent_runs.py` 提供 `/process` 快照、`/events` SSE 和显式 `/resume`；旧公共 `/advance` 已移除。原幂等提交、停止、确认、重试和重新生成调用方均已迁移。
- `useProcessStreams.ts` 管理 EventSource；`ResearchProcess.vue` 展示过程与当前输出；`AssistantMarkdown.vue` 统一渲染流中和历史 Markdown。
- 迁移 `0025_agent_process_stream` 增加过程表与 `INTERRUPTED` 状态，不清空已有 Run。部署仍要求单个 Backend 进程。

## 模块职责

```text
提交消息 / 回答补充 / 确认 / 重试 / 重新生成
    → 原业务受理与幂等校验
    → 后端运行托管器
        → AgentRuntime（状态、预算、权限、工具、最终回答）
            → SdkAgentLoop（复用 SDK 的模型与工具循环）
            → 用户过程投影（正文、思考、工具事实）
                ├─ 数据库过程内容快照
                └─ SSE 订阅 → Vue 当前输出 + 可折叠过程
```

运行托管器管理后台任务引用、异常收尾和应用生命周期；数据库 version/claim 继续确保唯一推进者。订阅连接不持有唯一的 SDK 消费循环，断开订阅不能取消后台执行。

受理成功后由后端调度原任务，前端不再依赖第二次 `/advance` 才真正启动。否则用户在“受理成功”和第二个请求之间关闭页面，仍无法满足 Q3。内部 `advance` 保留为执行方法，公共旧入口已移除，调用方和测试已一起迁移。

第一版沿用本地单实例服务边界；不新增 Redis、分布式 Worker 或全量 Event Sourcing。若部署改为多个独立 Backend worker，进程内任务托管与订阅广播必须重新设计。

## SDK 适配与权威状态

优先使用已安装 SDK 提供的流式输出，不自写 DeepSeek wire parser，不为了流式展示切换 Provider API。稳定的平台事件必须隔离 SDK 的版本、节点名和内部消息结构。

本次使用经典 `agent.astream(..., stream_mode=["messages", "updates", "values"], version="v2")`，在现有 `SdkAgentLoop` 中消费和规范化。离线探针已经核实：它能经过当前 `wrap_model_call + asyncio.create_task` 形态传出正文、`reasoning_content` 和工具参数增量，完整 `AIMessage.content` 仍为字符串，现有校验可保留。

新版 `astream_events(version="v3")` 也能返回独立正文和思考流，但当前有 experimental 标记，并会把完整 `AIMessage.content` 转成内容块列表，增加现有校验和续轮适配的改动。第一版选择能满足需求、与当前合同更接近的 v2；不把 SDK 协议版本暴露给前端。

当前 Provider 工厂显式传入 `streaming=False` 时，SDK 流式 Agent 接口不会自动覆盖。因此还需通过模型适配层明确开启本次 Agent 调用的 `streaming=True` 与 `stream_usage=True`，包括 `before_model` 重新创建模型的路径；不能通过修改 `requires_streaming_for_reasoning` 能力声明来间接启用。

`messages` 用于即时增量，`values` 用于收集完整状态。v2 的 values part 把暂停信息放在顶层 `interrupts`，不在 `data` 内；结束消费时必须规范化成 Runtime 当前使用的 `__interrupt__` 返回合同，不能只返回最后一份 `data`，否则会把 `ask_user` 暂停误判成最终回答。`updates` 可辅助观察图更新，但真正的工具事实仍来自 Runtime 的业务提交。

另已用项目 `ChatDeepSeekReasoning` 配合 `httpx.MockTransport` 模拟两轮 DeepSeek SSE：工具调用后再回答，分片、usage 和第二轮请求的 `reasoning_content` 回传均通过。该验证没有访问真实 DeepSeek，不能替代 Provider 验收。

保持 `before_model/on_model/on_tool` 等业务边界。模型完整响应、预算核算、原生工具协议、`ask_user` 中断、确认与恢复继续由现有 Runtime/SDK 负责；流式展示不能提前执行半截工具参数。

三个完成概念必须分开：

1. Markdown 段接收结束：渲染器可以收束语法；不表示它是最终回答。
2. 模型调用完成：完整消息可能仍携带工具调用，因此可能继续运行或等待用户。
3. 正式回答已提交：现有回答记录和 Run 成功状态一起提交后，前端才展示完成状态和回答版本操作。

最后一种情况下，以提交后的正文替换实时预览，包含后端追加的必要单位说明；不能把它再追加一遍造成重复。

## 过程内容与历史

新增独立的过程内容快照，不把每个 token 变成一条聊天消息，也不把 SDK checkpoint 作为永久过程档案。

- 过程段归属原 `AgentRun`，关联现有模型 `call_id` 或工具调用身份，拥有稳定段身份、顺序和修订号。
- 内容类别区分模型思考、助手正文和工具事实；段状态分为 streaming、complete、interrupted；失败原因仍由 Run/工具状态表示。
- 正文另有展示用途：尚未归类、过程说明、正式回答引用。用途变化不创建第二份可见文字。
- 工具事实引用已有执行与 Observation 记录，通过公开投影显示名称、状态、摘要和附件，不复制私有参数与原始结果到普通界面。
- `ask_user` 问题和工具确认保留现有专用消息/交互，在主区域呈现，不藏进折叠过程。
- 思考和正文分别累计；工具调用参数仅用于内部组装与校验。界面可在工具名确定时显示“准备调用”，实际派发后才显示“正在执行”。

流中按批次保存累计内容快照，模型完成、等待用户、失败、中止时保存边界状态。中途快照间隔设为约 1 秒并结合内容变化合并，避免逐 token 写库；突然断电可能丢失最后一个尚未保存的小段，正常停止尽力保存已接收的安全片段。最终间隔应以数据库写入和用户体验验证为准。

保存的是可公开的原始 Markdown 内容，渲染器临时补齐的括号、反引号、链接占位符不得写回记录、复制为模型原文或送回模型。Provider 续轮所需的原始 reasoning 留在 SDK 消息路径，不用经过展示修补的字符串替换。

重新生成的过程关联该次新 Run/回答版本，不能覆盖原版本的过程。删除对话时，新增过程记录纳入原有归属校验和删除流程。

## SSE 与重连

提供原 Run 的过程快照读取和只读 SSE 订阅，继续沿用当前 Actor/Conversation 访问校验。控制动作仍使用已有 POST 业务请求。

实际事件合同：

| 事件 | 内容 |
| --- | --- |
| `snapshot` | 当前 Run 的完整公开过程和公开状态；每次连接均发送，包括带旧 Last-Event-ID 的连接。 |
| `process.updated` | 变更段的**累计完整内容**与修订号，以及当前公开 Run。不是字符追加指令。 |
| `settled` | 后台本轮已收尾，Run 处于等待/终结/中断状态，客户端应关闭订阅。 |
| 心跳注释 | 空闲约 10 秒发出，维持连接。 |

实施时简化了讨论稿中的短期事件重放：**不维护 token 事件缓冲，重连总是以内容快照恢复**。这满足已确认的“保留内容、不保留逐字轨迹”，也减少游标过期和重复拼接状态。事件 `id` 仅标识当前连接中的传输顺序；客户端不依赖它恢复字节位置。

每个进程内过程通道有 epoch、文档 revision，段有稳定 segment_id、sequence 和 revision。先注册订阅再读取快照；前端按 epoch 和段 revision 替换/归并累计内容，换对话后忽略旧连接事件。每个订阅只保留一个唤醒通知，约 40ms 合并更新，慢订阅者直接读取最新累计状态，不阻塞生成者。完成并保存的无订阅通道可从内存淘汰，历史从数据库加载。

SSE 设置正确的内容类型、心跳、缓存与代理缓冲配置；通过实际 Vite → FastAPI 链路验证首段及时到达。等待补充、等待确认和任务完成由事件表达，不能靠连接关闭推断。前端在不再需要订阅时主动关闭连接，避免 EventSource 对已完成任务无限重连。

## 中断与恢复

增加非终态 `INTERRUPTED`，与失败/停止的 `TERMINATED` 分开；保留 checkpoint 供用户触发恢复。新增状态必须同步到业务转换、API、持久化查询、对话活动检查和前端。

启动恢复先识别旧进程留下的活动任务，保存中断过程和预算核算，不直接开始新的模型调用。原本等待用户补充或确认的任务保留等待语义。

用户点击恢复时，使用幂等请求并校验 Run 版本，先核查旧工具回执：已完成则复用结果；尚无可验证回执时保留中断状态并提示稍后继续，禁止自动重派。新一次模型调用形成新的尝试记录，保留旧片段的中断标记。无法安全恢复时明确显示原因，不能伪装成成功或默默从头执行。

用户也可以停止处于可恢复中断态的任务；已派发 GPU/ML 的迟到结果仍沿用原回执保存机制。

## 前端展示与 Markdown

新增 `ResearchProcess`、`AssistantMarkdown` 和流订阅 composable；现有 `ChatMessage` 保留消息、附件及回答版本的职责，`AgentRunCard` 继续承载等待、停止、恢复和错误操作。

`AssistantMarkdown` 的接口只接收公开原文、所属消息是否接收完成、正文/思考样式变体，不依赖 SSE 包或 Run 全对象。历史与实时内容复用同一组件。展开思考使用较弱的视觉层级，正文保持主要阅读区域；内部身份仅用于状态归并，不展示给用户。

运行时显示最新状态和可展开的详细过程；结束时默认折叠，保存用户主动展开的选择。等待输入和确认始终可见。用户向上滚动阅读时不强制拉回底部。

第一版已锁定 `remend@1.4.0 + markdown-it@15.0.2 + @mdit/plugin-katex@1.1.3 + katex@0.18.9`。KaTeX 0.18.11 被发布方标记为破坏性误发布，因此选用该插件兼容且未弃用的 0.18.9。公式解析支持 `$`、`$$`、`\(`、`\[`；完成前只显示公式源码。

| 比较 | remend + markdown-it | markstream-vue 2.0.16 |
| --- | --- | --- |
| 职责 | remend 修补临时显示字符串，markdown-it 解析成 HTML；Vue 展示策略由本项目封装。 | Vue 流式组件，提供节点渲染、表格、代码和可选数学等能力。 |
| 流式机制 | 每次仍处理完整输入，没有跨调用增量 AST；需要按活动段批量刷新，缓存完成段。 | 有增量解析与稳定节点复用，小样例实测一次 full、三次 tail；不能据此假定所有输入都只增量解析，未做本项目性能基准。 |
| 完成态 | 绕过 remend，直接解析保存的原始完整 Markdown。 | `final` 表示此段 Markdown 已接收完，不表示 Agent 的最终回答。 |
| 本次实测 | 20 组 Node 解析样例，覆盖半截强调、链接、图片、代码、表格、公式和危险协议。 | 同版本 Vue/Vite 的严格类型检查与生产构建通过；SSR 检查覆盖流中/完成态和 22 项 HTML 攻击语料。 |
| 当前限制 | 不完整链接/图片占位协议需要专门处理；公式插件、样式与刷新调度由项目负责。 | 当前发布包完成态仍吞掉部分独立/尾部标记；公开 streamParse 配置不能规避，需要等待上游发布修复或自行修补。 |

选择前者的依据是当前已发布版本的完成态内容保真和已有需求范围，而不是认为专用 Vue 流式组件不适合。暂不引入自定义补丁、从上游未发布 main 安装，或为几个尾部字符增加两套渲染器 fallback。

`markstream-vue@2.0.16` 的实测例子：完整输入 `|` 和 `$` 的最终输出丢失标记；`**bold***` 丢失粗体后的一个普通 `*`。在 `final:true` 搭配 `streamParse:false/auto/true` 时均复现。上游源码对 marker-only token 的处理有后续变更，本项目应在包含修复的正式版本发布后重新评估，不能把 main 上的修复当成已发布能力。

对照验证中，markdown-it 15.0.2 直接渲染原文 `|`、`$`、`*italic**`、`**bold***`，输出 HTML 分别保留 `|`、`$`、`italic*`、`bold*`，未复现这类丢失；这里是 Node HTML 结果检查，不是浏览器 DOM 验收。

第一版只启用已确认功能；不因渲染库自带能力而自动开放 Mermaid、自定义 HTML/Vue 组件或代码执行。

组件处理规则：

- 流式渲染只对当前活动段按帧或短时间批次更新；已完成段缓存。不要简单按空行切 Markdown，引用、列表和表格存在跨块语义。暂不承诺整体 O(增量长度) 性能。
- remend 只处理显示副本，结束后对原文完整解析；中断内容即使使用修补预览，也继续标注未完成。
- 使用 `html:false`；不完整链接使用 `linkMode:"text-only"`，不完整图片通过图片 renderer 显式处理。不能把 `streamdown:incomplete-link/image` 当成可点击/可请求的普通 URL。
- remend 的数学选项只补 `$`/`$$`，不会补 LaTeX 命令和花括号。流中不渲染数学，所属消息接收完成后才运行数学插件；完整但非法的公式保留源码或友好占位，不阻断整条回答。
- 数学格式支持 `$...$`、`$$...$$`、`\(...\)` 和 `\[...\]`；不通过无上下文全局替换误改代码块或金额。可在模型格式提示中约定基线定界符，这不用于识别回答是否结束。
- 复制、历史保存和回答版本使用公开原文，不复制 remend 临时补齐后的字符串。代码高亮按需加入，先确保代码内容、换行和表格可读。

不执行 LLM 提供的 HTML，限制危险 URL。第一版的结果图片继续使用原受控附件展示；Markdown 图片节点只显示替代文字，不自动请求模型给出的图片 URL，不能把该 URL 当作附件授权。

## 现有私有值边界

现有 `protect_text` 针对已知私有值执行完整字符串检查。逐 chunk 独立替换不能覆盖跨 chunk 的私有引用，直接把原始 delta 传给浏览器也会绕过现有发布边界。

实现时为每个文本/思考段维护有界尾部缓冲，在完整匹配跨片段私有值之前不发布相应前缀，脱敏后的公开内容才进入 SSE 与过程存储；完整消息仍经过现有校验。段结束、失败和停止都要处理尚未发布的尾部，宁可隐藏可疑残片，也不在错误收尾时直接释放原文。安全缓冲的长度与匹配规则需根据当前私有值集合验证，而不是假定固定几个字符足够。

工具状态文案从确定性执行事实产生。此处不做科学结论真实性判定，也不追加最终回答 Validator。

## 实施顺序与验证

1. 固定 SDK 和 Markdown 适配选择；用离线夹具验证文本/思考/工具分片、完整消息类型和格式渲染。
2. 添加过程持久化及迁移、运行托管和恢复状态；验证受理后断开、重复提交、停止、重启和工具不重复派发。
3. 添加 SSE 快照/增量/重连合同；验证乱序、重复、游标过期、慢客户端、完成后重连、跨段私有值保护。
4. 前端接入过程区与 Markdown；验证正文归类、历史一致性、等待交互、回答版本关联、折叠偏好、代码块/表格/公式和恶意链接。
5. 在真实 PostgreSQL 和浏览器下验证网络断开、刷新及后端重启；再以真实 DeepSeek 进行含工具续轮和 reasoning 回传的流式验收。Mock 工具通过不代表真实 GPU 模型通过。

## 验证与限制

实施验证记录见 [流式功能验收](../acceptance/streaming-research-process.md)。此前的选型探针仍用于说明库取舍，不代替本次集成验证。

过程内容是可解释性展示，不是科学证明；不新增最终回答 Validator。内部 ID 脱敏沿用已知私有值边界并增加跨分片缓冲，并非通用的任意敏感信息识别器。

主要参考：

- [LangChain 经典流式与事件流文档](https://docs.langchain.com/oss/python/langchain/streaming)
- [LangGraph 流式与状态文档](https://docs.langchain.com/oss/python/langgraph/streaming)
- [DeepSeek 思考与工具续轮说明](https://api-docs.deepseek.com/guides/thinking_mode/)
- [remend 1.4.0 固定源码](https://github.com/vercel/streamdown/tree/08da224d303e1ef2516e2be4ce04d0c55fbd68ea/packages/remend)
- [markdown-it 15.0.2 固定源码](https://github.com/markdown-it/markdown-it/tree/3c51991c32aaa2b002a52c009334ebe5752c84b3)
- [Markstream 安全配置](https://markstream.simonhe.me/guide/security)
- [markstream-vue 2.0.16 发布源码](https://github.com/Simon-He95/markstream-vue/tree/a0ff7cc192a4b214e177fd614afc3f6d0aed587c)
- [Markstream 最终态保留标记的上游修复 PR-781](https://github.com/Simon-He95/markstream-vue/pull/781)
- [Harness 类型化流与消息结算](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/agent-loop/src/assistant-stream.ts)
- [Harness 过程与答案投影](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/client/ui-chat/src/client/conversation-nodes/turn-process.ts)
- [Harness Markdown 渲染](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/client/ui-primitives/src/markdown/MarkdownText.tsx)
