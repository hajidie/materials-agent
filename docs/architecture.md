# 架构与数据流

本文解释现有代码怎样完成一次聊天任务，以及修改时需要守住的边界。具体字段和接口以当前路由、Pydantic schema、迁移及合同测试为准；本文不复制完整 API 清单。

## 系统组成

```text
Vue 聊天界面（frontend/）
        │ HTTP /api/v1
        ▼
FastAPI Backend（backend/，Python 3.11）
   ├─ PostgreSQL：对话、消息、AgentRun、工具来源与 SDK checkpoint
   ├─ MinIO：上传图片和生成附件
   ├─ Mock Runtime（Python 3.11）或真实 ZTA35G/EBSD Runtime（Python 3.8）
   └─ 可选 Materials ML Service / Worker（独立 Python 3.11）
```

Backend 是业务协调者；真实 Runtime 是旧模型和 GPU 依赖的隔离层。ML Service 有独立的存储与 Worker，不负责推进平台的 AgentRun。代码入口分别在 `backend/src/materialsagent/main.py`、`zta35g-runtime/src/` 和 `services/materials_ml/src/`。

## 一次聊天如何完成

1. 前端从 `ChatComposer` 提交消息和可选附件。`POST /api/v1/conversations/{id}/messages` 先返回接收回执；前端随后推进并查询对应 AgentRun。上传不确定时使用原幂等键核查，避免重复写入。
2. `AgentRuntime` 从对话、Run 和授权资源构造受控上下文。`ContextFramework` 只投影本次模型需要的事实；模型看到临时资源引用，不直接看到内部存储路径、凭据或完整原始结果。
3. LangChain `create_agent` 使用 Provider 原生工具调用续轮；LangGraph checkpoint 保存 SDK 消息和暂停游标。业务校验、预算、授权和执行权仍由 Backend 的 Agent Runtime 与 Tool Registry 控制。模型可调用 `ask_user` 暂停；用户回复后恢复原 Run。
4. 工具请求经参数、资源身份、权限和状态校验后执行。Standard Tool 记录 Invocation；Managed Tool 还记录 Task、InputRevision、ToolRun、ToolResult 与附件来源。已验证的 Observation 返回模型继续处理。
5. 模型不再请求工具时，其完整助手消息作为最终回答。Backend 将回答、可信来源和 Run 成功状态提交；前端把进度、消息及结果附件呈现在同一对话中。

相关实现主要位于 `backend/src/materialsagent/application/agent_runtime.py`、`sdk_agent_loop.py`、`context_framework.py`、`tools.py` 和 `frontend/src/composables/`。

## 状态、失败与重试

`AgentRun` 是业务状态的权威记录，checkpoint 只负责模型续轮。Run 可等待用户补充或确认；短事务、版本和 claim 控制唯一推进者。外部 LLM、Runtime、MinIO 调用不应跨越数据库长事务。已派发操作发生断连或超时时，只核查原回执，不因为请求失败而重新派发。

Stop 取消后续模型决策；已派发的 GPU 或 ML 工作可能继续，迟到结果仍保存。失败工具的显式重试创建新的执行尝试；结果不确定时不能假设可安全重试。回答重新生成使用原任务和已验证结果，禁止调用工具，并保留旧回答版本。删除对话先检查运行和资源状态，再通过持久化清理记录处理本项目对象。

## 工具与资源边界

默认 Registry 注册单位换算、ZTA35G SEM 虚拟实验和 EBSD 屈服强度预测；可选 ML 工具由配置启用。真实 SEM 与 EBSD 共用 Runtime 执行资源，Backend 不加载权重，也不自动重试模型执行。EBSD 输入图片是来源，不是生成结果；结果必须保留对应 ToolRun、单位和适用限制。

ML Engine 只做数值表格回归，目前支持 LR/RF。Service 管理数据、训练和预测，Worker 通过 Service API 交回结果。平台只持有受控资源引用；模型对资源名称和单位的推断须由 Backend 对最终绑定资源再次校验，未知单位在数值换算前需要用户明确确认。

## 界面边界

前端以聊天为唯一工作入口：问题在原输入框补充，运行状态和结果跟随消息展示，附件详情通过只读 Viewer 查看。查看或下载不改变工具输入、草稿或 Agent 状态。界面文案使用用户能理解的进度和错误，不暴露内部 ID、原始 JSON 或调试异常。样式与响应式规则以 `frontend/src/styles.css` 和组件测试为准。
