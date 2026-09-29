# 验证指南

先用最接近改动的测试验证行为，再根据影响的合同扩大范围。测试文件存在不代表本次已运行，更不代表真实模型或浏览器链路通过。仓库没有统一的覆盖率百分比门槛。

## 常用命令

在仓库根目录运行；Python 命令必须使用对应环境。Backend 与 Mock Runtime 分开执行，因为两侧都有同名的 `test_ebsd.py`。

```powershell
# materialsagent-backend，Python 3.11
python -m pytest backend/tests/unit/test_agent_runtime.py -q
python -m pytest backend/tests -q
python -m pytest mock-runtime/tests -q

# 前端，Node.js 24.14.x / npm 11.9.x
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build

# materialsagent-zta35g，Python 3.8；不加载真实权重
python -m pytest zta35g-runtime/tests/unit zta35g-runtime/tests/contract -q

# 独立 ML Python 3.11
python -m pytest services/materials_ml/tests/test_engine.py -q
```

Backend 的 `api/` 与 `integration/` 测试可能使用真实 PostgreSQL；存储集成测试使用 MinIO。先按 [README](../README.md#本地启动) 准备本地依赖。前端 Vitest 使用 jsdom 和请求替身，不等于浏览器或真实 Backend 验收。

统一脚本 `scripts/acceptance/run-agent-acceptance.ps1` 依次执行 Backend、Mock Runtime、前端及本地脚本检查，要求 Backend Python 3.11 和已就绪的数据库、对象存储；其中的“离线”只表示不调用真实 Provider/GPU。独立 ML 的 Service/Worker/MCP 验收入口和配置见 [ML 服务说明](../services/materials_ml/README.md)。

## 按风险选择验证

| 改动 | 至少检查 |
| --- | --- |
| 局部 Python 逻辑 | 直接相关的 `test_*.py`；缺陷修复添加回归用例 |
| API、工具合同、状态或迁移 | 相关 unit、contract、API 和真实数据库用例；必要时扩大至 Backend 全量 |
| Vue 组件或交互 | 对应 `*.test.ts`；共享类型运行 typecheck，打包相关改动运行 build |
| 本地启动脚本 | `scripts/dev/test-local-dev.ps1`；范围检查脚本改动运行 `test-check-scope.ps1` |
| 跨服务 ML 或真实模型 | 对应独立环境的验收；分别记录 Service、MCP、GPU 和浏览器结果 |

最后运行 `git diff --check`，检查实际 diff 与未提交文件，并记录未执行的验证及原因。文档改动至少核对命令、路径和相对链接。

## 证据能说明什么

- Mock LLM/Runtime 验证协议、状态与错误处理；它不证明自然语言理解或材料预测准确性。
- 真实 Provider 验证模型选工具和补参行为；若工具仍是 Mock，不能据此宣称真实 SEM、EBSD 或 ML 计算通过。
- 独立 Runtime 的 GPU 测试验证模型适配和产物合同，不覆盖平台持久化、恢复或界面操作。
- 浏览器验收检查真实点击、键盘、焦点和窄屏布局；jsdom 断言不能替代它。科学准确性还需要独立数据与评估。

测试命名沿用现有约定：Python 为 `test_*.py`，前端为 `*.test.ts`。
