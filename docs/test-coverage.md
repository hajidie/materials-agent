# 需求与风险到测试的映射

本文件维护测试资产的保护目标与证据层级，不记录测试数量、逐轮验收结果或服务运行状态。
产品合同见 [README](../README.md) 与 [项目上下文](project-context.md)；测试执行规则见
[AGENTS.md](../AGENTS.md)。表中列出代表入口，同组其他失败窗口和边界用例仍属于保护资产。

## 如何解释覆盖

- Unit、Component、API、Integration、Contract、E2E 描述验证层级；Regression 和历史兼容是附加标签。
- `backend/tests/api/conftest.py` 的 `api_harness` 使用真实临时 PostgreSQL；API 目录不等于无外部依赖。
- Frontend 的 Vitest 使用 jsdom；App 组合测试的 fetch 为替身，不能证明浏览器或 Backend 链路。
- HTTP/MCP 服务链路可以连接真实数据库、存储与 Worker，同时使用脚本模型；这不证明自然语言能力。
- 真实 Provider、模型/GPU与浏览器验收必须分别运行。测试资产存在、执行通过和当前服务可用是不同事实。
- 类型断言由 `npm --prefix frontend run typecheck` 检查；jsdom 中的 CSS 类名断言不证明实际布局。

## 核心保护目标

| ID | 用户行为或必须守住的规则 | 防止的风险 | 代表测试入口 | 证据边界 |
|---|---|---|---|---|
| CONV-01 | 新建、读取 Conversation，重复提交恢复同一结果 | 重复创建、归属泄漏、并发覆盖、丢失响应后重复操作 | [Conversation API](../backend/tests/api/test_conversations.py)、[幂等持久化](../backend/tests/integration/db/test_idempotency_persistence.py)、[App 流程](../frontend/tests/components/app-flow.test.ts) | API+真实 DB；组件 fetch 为替身 |
| RUN-01 | 有界 Decision→Tool→Observation→Decision；补充信息恢复原 Run | 重复派发、错误等待版本、重复抽取、预算重置 | [Runtime](../backend/tests/unit/test_agent_runtime.py)、[Agent API 控制](../backend/tests/api/test_agent_control.py)、[连续编排](../backend/tests/api/test_message_orchestration.py) | Unit 与 API+DB；SEM Runtime/模型为替身 |
| RUN-02 | 确认绑定 Invocation 与参数版本，拒绝或过期不得执行 | 旧参数获准、重复确认产生副作用 | [确认 Unit](../backend/tests/unit/test_tool_invocation_service.py)、[确认 API](../backend/tests/api/test_agent_control.py)、[真实 MCP 链路](../services/materials_ml/tests/service/test_platform_mcp.py) | API 默认目录用开发工具；ML 链路用真实服务和脚本模型 |
| RUN-03 | 显式失败重试创建新执行；再生成回答禁止 Tool | 自动重试、重写旧结果、再生成触发推理 | [执行服务](../backend/tests/unit/test_tool_execution_service.py)、[控制 API](../backend/tests/api/test_agent_control.py) | Unit/API+DB；保留尝试、seed 和零派发断言 |
| RUN-04 | 提交成功后断连不撤销事实；重启仅修复已提交结果 | 不确定提交重复执行、半成品被当成功、外部调用持有长事务 | [控制 API](../backend/tests/api/test_agent_control.py)、[结果事务](../backend/tests/integration/db/test_result_commit.py) | 真实 DB 与注入的失败窗口；不同窗口不互相替代 |
| CTX-01 | 权威事实白名单投影、调用内引用解码、审计不保存临时引用 | 内部 ID 泄漏、无关历史污染、幻觉引用、误绑定唯一候选 | [Context](../backend/tests/unit/test_context_framework.py)、[资源上下文](../backend/tests/unit/test_ml_resource_context.py)、[Runtime 审计](../backend/tests/unit/test_agent_runtime.py) | `test_persisted_action_drops_call_scoped_resource_handle_before_binding` 经 advance 检查存储状态；Store/Tool 为替身 |
| BUDGET-01 | Provider 输出额度受剩余预算约束，Usage 完整计费 | reasoning 重复计费或漏计、缺失 Usage 被当作零、超预算调用 | [模型适配](../backend/tests/unit/test_agent_model.py) | `test_full_usage_observable_components_are_not_lost_or_double_counted` 覆盖 native 与 LangChain 格式；不调用真实 Provider |
| TOOL-01 | Registry 是唯一授权与 schema 来源，异构工具经统一执行 | ZTA 特例侵入通用执行、schema 漂移、伪造注册与授权 | [Registry](../backend/tests/unit/test_tool_registry.py)、[异构组合](../backend/tests/integration/test_tool_registry_extensibility.py)、[执行服务](../backend/tests/unit/test_tool_execution_service.py) | `test_generic_execution_dispatches_ml_fixture_with_immutable_provenance` 保护异构执行与持久化来源；夹具为替身 |
| TOOL-02 | 已退出的独立执行路由不得因开发开关恢复 | 绕开 Agent Runtime 的平行执行入口 | [路由合同](../backend/tests/api/test_tools.py) 的 `test_retired_tool_execution_route_is_absent` | 不使用 DB；同时验证开关开、关、HTTP 404 与 OpenAPI 中路径缺席 |
| RESULT-01 | 一个 ToolRun 的参数、图片、数值、结果来源保持一致 | 混合不同尝试、产物篡改、失败结果填入虚假性能 | [结果领域](../backend/tests/unit/test_tool_result_domain.py)、[结果事务](../backend/tests/integration/db/test_result_commit.py)、[输出合同](../backend/tests/contract/test_tool_execution_output.py) | Unit/真实 DB/Contract；不可用单条成功测试替换完整性门禁 |
| ASSET-01 | 上传、读取、补偿与恢复维护精确对象身份 | 未知写入被重发、误删外部对象、坏图片被冒充成功 | [存储合同](../backend/tests/unit/test_storage_contract.py)、[MinIO](../backend/tests/integration/storage/test_minio_storage.py)、[Asset 生命周期](../backend/tests/integration/storage/test_asset_lifecycle.py)、[图片交互](../frontend/tests/components/asset-gallery.test.ts) | 替身失败注入与真实 MinIO 分别提供证据；实际图片布局另行视觉验证 |
| DELETE-01 | 删除 Conversation 拒绝活动工作，事务后受限清理 | 迟到写入、越界清理、清理失败不可恢复 | [cleanup](../backend/tests/api/test_conversation_cleanup.py)、[删除 fence](../backend/tests/integration/db/test_ml_fence.py)、[ML 资源链路](../services/materials_ml/tests/service/test_platform_resources.py) | API/DB/真实 ML HTTP；监督浏览器清单不单独证明删除 |
| EBSD-01 | 合规单图、来源绑定、缺图补充、BUSY 显式重试 | 非法图推理、跨对话引用、输入图被列为生成产物、重放多次执行 | [EBSD API](../backend/tests/api/test_ebsd.py)、[组件](../frontend/tests/components/ebsd-flow.test.ts)、[Runtime 合同](../zta35g-runtime/tests/contract/test_ebsd_http.py)、[GPU 验收](../zta35g-runtime/tests/compatibility/test_ebsd_http_gpu.py) | API 使用 DB+Mock Runtime+内存存储；GPU 仅验证独立 Runtime，不是平台 E2E |
| RUNTIME-01 | 固定推理合同、鉴权、共享锁、单次执行与数值失败 | 参数漂移、BUSY 排队或自动重试、NaN/Inf 被修补、错误权重加载 | [Backend 适配合同](../backend/tests/contract/test_runtime_contract.py)、[真实 HTTP 适配](../backend/tests/contract/test_real_runtime_adapter.py)、[Runtime HTTP](../zta35g-runtime/tests/contract/test_runtime_http.py)、[推理](../zta35g-runtime/tests/unit/test_inference.py)、[模型兼容](../zta35g-runtime/tests/compatibility/test_model_loading.py) | HTTP server 可用假模型组件；真实权重/GPU需独立授权。逐步数值失败行为覆盖替代到位前保留 AST 守卫 |
| ML-01 | 无数据泄漏的训练评估、明确 schema 与可信模型包 | 错误指标、训练使用测试数据、损坏或不可信包被反序列化 | [Engine](../services/materials_ml/tests/test_engine.py)、[Package](../services/materials_ml/tests/test_package.py) | 独立 ML 环境，实际 LR/RF 与模型包；不涉及 Agent 语义 |
| ML-02 | Service 独占领域写入，Worker/Prediction 可取消和恢复 | 跨 scope 操作、发布不原子、重复训练/预测、孤儿子进程 | [Resources](../services/materials_ml/tests/service/test_resources.py)、[Publication](../services/materials_ml/tests/service/test_publication.py)、[Prediction](../services/materials_ml/tests/service/test_predictions.py)、[恢复](../services/materials_ml/tests/service/test_prediction_recovery.py)、[真实链路](../services/materials_ml/tests/service/test_end_to_end.py) | 真实 PostgreSQL/MinIO与 Windows 子进程；隔离测试资源，禁止清空共享卷 |
| ML-03 | 单位权威与推断分离，数值使用前确认未知单位 | LLM 覆盖登记单位、确认丢失、模型/数据集单位串用 | [单位策略](../backend/tests/unit/test_unit_resolution.py)、[持久化展示](../backend/tests/api/test_chat_artifacts.py)、[平台 MCP](../services/materials_ml/tests/service/test_platform_mcp.py)、[前端](../frontend/tests/components/ml-resources.test.ts) | 高价值风险保护；无独立历史 Bug 证据时不标为已发生 Bug |
| ML-04 | 资源引用与歧义交给模型，冻结选择后复核权威身份 | 唯一候选自动绑定、确认后资源变化、资源服务不可用仍派发 | [资源上下文 DB](../backend/tests/integration/db/test_ml_resource_context_db.py)、[语言链路](../services/materials_ml/tests/service/test_platform_resource_context.py) | 脚本模型和真实 Provider 验收分开；真实 Provider 需 `P6_REAL_LLM=1` |
| UI-01 | 对话中安全提交、恢复、观察结果，保持历史回答不可变 | IME 误提交、跨对话草稿串用、重复提交、查看触发写入、模型 HTML 执行 | [Composer](../frontend/tests/components/chat-composer.test.ts)、[App](../frontend/tests/components/app-flow.test.ts)、[ML 附件](../frontend/tests/components/ml-resources.test.ts)、[监督浏览器](../services/materials_ml/tests/service/test_platform_resource_browser.py) | 组件层与浏览器分开；浏览器需要 `P7_BROWSER_ACCEPTANCE=1` 和人工操作证据 |
| UPGRADE-01 | 历史数据迁移与合同升级安全，拒绝不可信回填 | 数据损坏、危险降级、旧入口/旧语义复活 | [迁移](../backend/tests/integration/db/test_migrations.py)、[合同升级](../backend/tests/api/test_chat_upgrade.py) | 历史术语不代表失效；评估升级/降级支持合同后才能退役 |
| OPS-01 | 安全启动、依赖就绪、精确进程归属与改动授权 | Secret 暴露、误杀进程、假 ready、目录授权扩大 | [健康](../backend/tests/api/test_health.py)、[配置](../backend/tests/unit/test_config.py)、[启动离线测试](../scripts/dev/test-local-dev.ps1)、[范围离线测试](../scripts/dev/test-check-scope.ps1) | 离线测试不证明当前服务运行或本地栈启动成功 |

## 行为覆盖矩阵

下表表示现存测试资产，不表示本次执行通过。`✓` 为直接断言，`链路内` 为组合流程经过该行为，
`—` 为尚无对应层级证据；“真实 HTTP/MCP”使用独立服务、数据库和存储，默认仍是脚本模型。
GPU 一列指真实权重的 Runtime 验收，不能替代平台或浏览器链路。

| 核心行为 | Unit/Contract | Component | API+DB | 真实 HTTP/MCP E2E | 监督浏览器 | 真实权重/GPU |
|---|---|---|---|---|---|---|
| 新建 Conversation | ✓ | ✓ | ✓ | 链路内 | 链路内 | — |
| 多轮上下文与资源指代 | ✓ | ✓ | ✓ | ✓（P6；Provider 单独 opt-in） | 链路内（Provider 单独 opt-in） | — |
| Tool 选择与授权 | ✓ | — | ✓ | ✓（ML 工具） | 链路内（ML 工具） | — |
| 参数补充与资源歧义 | ✓ | ✓ | ✓ | ✓（P4/P6） | — | — |
| 用户确认、拒绝与过期版本 | ✓ | ✓ | ✓ | ✓（P4/P6） | 链路内（确认） | — |
| Managed SEM 执行与结果来源 | ✓ | ✓ | ✓（Runtime 替身） | — | — | ✓（独立 Runtime） |
| Runtime BUSY 与显式重试 | ✓ | ✓ | ✓（Runtime 替身） | — | — | ✓（共享锁/HTTP） |
| EBSD 上传、缺图恢复与预测 | ✓ | ✓ | ✓（推理替身） | — | — | ✓（独立 Runtime） |
| ML 训练、预测与模型包下载 | ✓ | ✓ | ✓（远端替身） | ✓（LR/RF、真实 Worker） | ✓（合成数据） | — |
| 单位权威、未知单位确认 | ✓ | ✓ | ✓ | ✓（P4） | — | — |
| 查看附件只读、完成结果只追加一次 | ✓ | ✓ | ✓ | ✓（P7 HTTP） | ✓（P7） | — |
| 取消、Worker 死亡与服务重启恢复 | ✓ | — | ✓（平台失败窗口） | ✓（Service/Worker 子进程） | ✓（界面恢复） | — |
| Conversation 删除与资源 fence | ✓ | ✓ | ✓ | ✓（P5 关闭与迟到写入） | — | — |
| 历史数据库升级、降级与拒绝危险回填 | ✓（合同） | — | ✓（真实迁移） | ✓（P5 历史迁移） | — | — |

SEM/EBSD 平台真实模型完整链路，以及浏览器中的删除、歧义和单位确认没有直接验收资产，
不得用 Mock API、ML 的 E2E 或独立 Runtime GPU 用例填上这些空格。

## 真实集成与 E2E 的保留目标

| 入口 | 必须保护的真实问题 | 治理判断 |
|---|---|---|
| [DB 迁移](../backend/tests/integration/db/test_migrations.py) | 升降级保留行、约束、索引；拒绝不安全降级 | 保留。历史表名不是删除依据；现役迁移重建旧 schema 不完整时应暴露失败 |
| [MinIO 存储](../backend/tests/integration/storage/test_minio_storage.py) | 真实写入、读取、删除、身份冲突与幂等重放 | 保留。成功、未知写入、失效凭据和冲突不是重复 |
| [Service/Worker HTTP](../services/materials_ml/tests/service/test_end_to_end.py) | 训练与预测实际数值；取消停掉进程树；死亡/重启不重新训练；丢失回执不重复提交 | 保留 LR/RF、claim/start/complete 各失败窗口；不可合并为 HTTP 200 冒烟 |
| [真实 MCP SDK](../services/materials_ml/tests/service/test_mcp.py)、[平台 MCP](../services/materials_ml/tests/service/test_platform_mcp.py) | SDK/REST 回执一致；断连后 Worker 完成；Agent 确认与来源绑定 | 两层保留，SDK 互操作与平台授权是不同合同；单位来源检查定位 TOOL_RESULT，不能假设它是包含补参记录的 Observation 列表第一个元素 |
| [资源 HTTP](../services/materials_ml/tests/service/test_platform_resources.py)、[上下文 HTTP](../services/materials_ml/tests/service/test_platform_resource_context.py) | 上传身份、删除 fence、资源歧义、确认后冻结与权威复核 | 保留真实依赖与失败注入；移除无设置变化的重复 Backend 重启；问题措辞不限制真实 LLM；[脚本夹具](../backend/tests/support/ml_resource_language_model.py) 补参使用模型可见字段；能力关闭可在 Observation 生成前拒绝，保护目标是 ML 数据库没有新训练记录 |
| [聊天附件 HTTP](../services/materials_ml/tests/service/test_platform_resource_ui.py) | 下载内容正确、查看只读、完成只追加一次、旧 Run 不变、远端停机历史仍可读 | 用现役整个 UI 状态合同替换已删除的 selections 字段断言；保留 LR/RF 与预测数值比较 |
| [Prediction 集成](../services/materials_ml/tests/service/test_predictions.py) | 缺失特征拒绝发生在持久化与子进程派发前 | 加强 422 用例，检查零 Prediction 写入、零 runner 调用；纯凭据配置移至无基础设施的隔离合同测试 |
| [监督浏览器](../services/materials_ml/tests/service/test_platform_resource_browser.py) | 上传、聊天、只读查看、重复观察、下载、键盘、窄屏与恢复的可操作性 | 保留独立 opt-in；检查记录必须严格为布尔 true，字符串等 truthy 值不能证明操作通过 |
| [SEM GPU 产物](../zta35g-runtime/tests/compatibility/test_payload_and_resources.py)、[EBSD GPU](../zta35g-runtime/tests/compatibility/test_ebsd_http_gpu.py) | 固定推理参数、实际数值/图片合同、显存安全、权重不变和共享锁 | 保留专用门禁；产物摘要与实际文件内容核对，摘要长度不证明完整性 |

## Regression 的证据与治理规则

MCP 可用性探测与短缓存的回归入口是
`backend/tests/unit/test_mcp_executor.py::test_ml_catalog_health_probes_validate_each_pinned_mcp_binding`
与 `::test_mcp_readiness_is_safe_and_briefly_cached`，对应修复提交 `54c6c82`。
这类用例即使实现耦合，也应优先保留并改写，而不是删除保护目标。

删除或合并测试时，必须指出本表保护目标、保留的测试入口以及被替代的验证层级。
同一行为的 Unit、DB、HTTP、浏览器测试不自动构成重复；同一测试函数体搭配不同参数化数据也不自动重复。
成功、拒绝、并发、提交不确定和重启恢复是不同失败窗口。替代测试必须保留原有关键边界，
低层或全 Mock 测试不能替代真实依赖验证。未测量耗时前不宣称性能收益。

执行入口与隔离要求遵循 README、AGENTS.md 及各验收脚本。尤其 ML 集成需 `ML_INTEGRATION=1`，
平台链路需独立 `P4_BACKEND_PYTHON`；`acceptance-p7.ps1` 完成不等于监督浏览器验收完成。
