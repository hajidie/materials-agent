# M12-A 离线 Provider 审查修订验收报告

状态：`M12A_PROJECT_OWNER_ACCEPTED`

```text
Code review:
APPROVED

Offline acceptance:
APPROVED

Real Provider calls:
0

M12-B:
NOT STARTED

Commit:
AUTHORIZED SUBJECT TO STAGED-DIFF GATES
```

## Git 与授权基线

- branch / HEAD：`main` /
  `f5e24dcaab4801dbeffb8400f2960c33b60b4f00`
- HEAD subject / parent：`feat: complete phase 1a mock acceptance` /
  `4ed740222238433541fb31c993dd75610634d157`
- 本轮修订开始时：22 个 tracked modified、7 个 untracked，共 29 个
  M12-A allowlist 路径；staging empty。
- 本轮新增修改两个原已在 M12-A allowlist、此前尚未 dirty 的测试：
  `backend/tests/api/test_explanation_outcomes.py` 和
  `backend/tests/unit/test_explanation_service.py`。修订后共 24 个 tracked
  modified、7 个 untracked，共 31 个路径。
- 未清理、重置或覆盖既有 M12-A 实现；未修改依赖；未安装包。
- 精确暂存和唯一 M12-A 验收 commit 已获条件授权；push、amend、真实
  Provider 调用、M12-B 和 M13 均未授权、未执行。

## 审查问题修订

### Chat 非法 JSON 与 Schema 分类

- `parsing_error` 非空时，仅在内存中对 `raw.content` 执行一次
  `json.loads()`：语法非法映射 `LLM_INVALID_JSON`；JSON 合法但
  Pydantic/route/tool Schema 不符映射 `LLM_SCHEMA_MISMATCH`。
- `invoke()` 直接抛出的 `json.JSONDecodeError` 映射
  `LLM_INVALID_JSON`；受控 `OutputParserException` 和
  `ValidationError` 映射 `LLM_SCHEMA_MISMATCH`。
- `_domain_result()` 不再使用 `assert` 校验外部模型数据，必需值缺失使用
  显式校验并稳定映射 `LLM_SCHEMA_MISMATCH`，不依赖 `python -O`。
- 每次业务调用仍只有一次 `invoke()`；未增加 repair、retry、fallback 或第二次
  Provider 调用。raw content、parser 异常正文和 SDK 异常正文均不持久化。

### Explanation 内部错误与公开错误分离

`ExplanationOutcome` 新增可选的 `llm_error_code` 和
`llm_safe_error_message`。成功 outcome 要求公开与内部错误字段全部为 null；
失败 outcome 的公开错误必填，内部详细字段必须成对出现并保持受控、可打印和有界。

| Provider / 协议失败 | LLMCall 内部错误 | Explanation / Task / API 公开错误 |
|---|---|---|
| timeout | `LLM_TIMEOUT` | `EXPLANATION_TIMEOUT` |
| HTTP 401 | `LLM_AUTHENTICATION_FAILED` | `EXPLANATION_PROVIDER_UNAVAILABLE` |
| HTTP 402 | `LLM_BALANCE_EXHAUSTED` | `EXPLANATION_PROVIDER_UNAVAILABLE` |
| HTTP 429 | `LLM_RATE_LIMITED` | `EXPLANATION_PROVIDER_UNAVAILABLE` |
| HTTP 400 / 422 | `LLM_REQUEST_REJECTED` | `EXPLANATION_PROVIDER_UNAVAILABLE` |
| HTTP 500 / 503 / connection | `LLM_PROVIDER_UNAVAILABLE` | `EXPLANATION_PROVIDER_UNAVAILABLE` |
| 空响应 | `LLM_EMPTY_RESPONSE` | `EXPLANATION_PROTOCOL_ERROR` |
| Schema / 文本协议不符 | `LLM_SCHEMA_MISMATCH` | `EXPLANATION_PROTOCOL_ERROR` |
| 未知受控失败 | 对应受控 LLM 错误 | `EXPLANATION_FAILED` |

Provider 详细错误只进入 `LLMCall`。`NaturalLanguageExplanation`、`Task` 和
HTTP API 继续使用既有公开 Explanation 错误。旧 Mock outcome 的内部字段为 null，
`LLMCall` 继续回退使用公开错误，兼容既有数据和测试。

数据库参数化测试覆盖 401、402、429 和空响应；API 测试证明 401 细分类只写入
`LLMCall`，公开响应、Explanation 和 Task 均不含
`LLM_AUTHENTICATION_FAILED` 或其内部安全文案。

### Purpose 绑定的 generation parameters

- `CHAT_ORCHESTRATION` + DeepSeek 五键形状只接受
  `response_format=json_object`、`max_tokens=1024`。
- `TOOL_RESULT_EXPLANATION` + DeepSeek 五键形状只接受
  `response_format=text`、`max_tokens=768`。
- 两个交叉组合均由拒绝测试覆盖并抛 `ValueError`。
- Mock 既有两键 generation parameters 形状保持兼容；无 migration。

### Explanation 文本规范化

- Adapter 将 CRLF、CR、LF 和 tab 确定性替换为空格，合并连续空白并去除首尾空白。
- 其他 Unicode 控制字符继续失败关闭。
- 规范化后的文本必须非空、不超过 4096 字符、可打印且为单行。
- 两段正常文本成功用例和 NUL 控制字符失败用例均通过；没有额外 Provider 调用。

### Secret 与 Mock 懒导入

- 全空白 `DEEPSEEK_API_KEY` 仍可在 Mock 模式归一化为 None。
- 非空 Key 只要含首部或尾部空白即产生安全配置错误；不会静默 trim 后使用，错误
  不含 Key。
- 本轮进程环境中 `DEEPSEEK_API_KEY` 不存在；未读取 `.env`，未请求或使用真实 Key。
- 干净 Python 子进程 import `materialsagent.main` 并以 Mock 配置创建应用后，
  `deepseek_chat` 与 `deepseek_explanation` 均不在 `sys.modules`。
- 既有测试继续证明 Mock wiring 不构造 `ChatDeepSeek`。

## 离线与网络边界证据

- Real Provider calls：`0`。真实 Provider 调用未授权；所有 DeepSeek adapter
  合同测试均使用注入 fake Runnable/model，Phase 1A Runner 强制
  `LLM_ADAPTER=mock` 并以空值覆盖 Key。
- DeepSeek construction in Mock mode：`0`，由 wiring fail-if-constructed 测试证明。
- DeepSeek import in clean Mock subprocess：`0`，由独立子进程 `sys.modules`
  断言证明。
- Focused public-network guard：`PASS`，精确测试
  `test_fake_adapters_make_no_socket_attempt` 在阻断 `socket.connect` 时通过
  （`1 passed in 1.33s`）。该证据只覆盖注入 fake adapter 的聚焦调用路径，
  不泛化为完整进程级网络监控结论。
- Phase 1A Runner forced Mock：`PASS`。

## 自动化回归

- 全部审查点聚焦套件：`217 passed in 19.79s`。
- Chat contract 修订：生产修复前 `3 failed, 41 passed`；修复后所在两份
  contract 合集 `62 passed in 1.63s`。
- Explanation contract 修订：生产修复前 `17 failed, 45 passed`；修复后转绿。
- purpose 绑定：生产修复前 `2 failed, 55 passed`；修复后
  `57 passed in 0.13s`。
- Secret 空白边界：生产修复前 `2 failed, 52 passed`；修复后
  `54 passed in 0.24s`。
- Explanation DB：`28 passed in 12.20s`。
- Explanation API：`9 passed in 5.94s`。
- Mock/DeepSeek wiring：`5 passed in 4.05s`。
- Backend full：`978 passed in 186.40s`。
- Backend M11 E2E：`24 passed in 61.92s`。
- Mock Runtime：`11 passed in 0.93s`。
- Frontend Vitest：9 files / `204 passed in 4.38s`。
- Frontend typecheck：`PASS`。
- Frontend production build：`PASS`，39 modules。
- `pip check`：`No broken requirements found.`
- Alembic heads/current：`0009_timeline_query_indexes (head)`；
  check：`No new upgrade operations detected.`；
  revision consistency：`ALEMBIC_REVISION_CONSISTENCY_OK`。

## 权威 Phase 1A Runner

- run id：`20260728T141030Z-5d072a155237`
- status：`PHASE_1A_ACCEPTANCE_PASSED`
- failed：`0`
- 场景计数：success 1、input error 5、dependency failure 7、
  idempotency 5、retry 2、asset/security 2、browser manual 11。
- 动态 artifacts 安全扫描：`ACCEPTANCE_SECURITY_SCAN_OK`。
- Runner 在嵌套 `try/finally` 中强制 Mock、空 Key 并恢复调用者环境；本轮原变量
  均不存在。Runner 停止其启动的 Backend、Frontend、Runtime、PostgreSQL 和
  MinIO；结束后 3000/8000/8100 listener 均为 0，Mock state 文件不存在。

## Scope、SEM 与 Git

- `SCOPE_OK M12A`
- `SEM_INTEGRITY_OK`：57 files，
  `total_size_bytes=2043071133`，
  fingerprint
  `62bbb0878ed5d659490755e401fba0e3e09f1f36e3a67667ea04227927546b4a`
- 五份确认设计基线、migration、公共 API Schema、Frontend 源码、
  Mock Runtime 源码和 `SEM/` 均未修改。
- `git diff --check` / `git diff --cached --check`：`PASS`
- staging：empty
- 当前变更：31 个 M12-A allowlist 路径，unexpected=0。

## 已知限制与暂停点

M12-A 只证明离线合同、错误隔离、应用接线和 Mock 回归，不证明真实 Provider 的
模型可用性、响应质量、真实 header、配额或延迟。真实调用属于未授权且未开始的
M12-B。

最终状态：`M12A_PROJECT_OWNER_ACCEPTED`
