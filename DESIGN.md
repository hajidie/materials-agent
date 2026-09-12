# Materials Agent UI

本项目是中文材料研究工具，使用场景是桌面实验参数输入、长耗时模型执行和结果查阅；窄屏仍需可操作。
保留现有墨绿/灰绿界面、系统中文字体、左侧对话列表和右侧聊天内容，不引入新的视觉主题。

## Canonical UI Map

- 页面布局和颜色：frontend/src/styles.css；宽度、断点、滚动与 focus-visible 沿用现有规则。
- 新目标/补参输入：ChatComposer；Enter 发送，Shift+Enter 换行，IME composition 不发送。
- 运行、等待、确认、失败与最终答案：AgentRunCard；不以网络断连表示取消。
- 工具性能与条件：ResearchResultSummary；图像加载、错误、重试与下载：AssetGallery。
- 对话列表：ConversationSidebar；删除确认及焦点管理：DeleteConversationDialog。
- 写操作与幂等：useAgentRuns；不确定时保留原请求，禁止并发新写；查询不触发工具。
- 公共资产链接：api/client；只接受受控 Asset content 路径。

## 交互约束

切换对话与补参目标时隔离输入草稿。用户提交得到服务端确认后才清空文本；网络不确定保留草稿和原键。
等待状态提供显式恢复或确认；终态只读，失败重试与重新生成回答分别命名。结果与图像按 Observation
来源展示，不跨 ToolRun 混合。空态提供单位换算示例，加载态可见；错误可重试，所有文字按纯文本展示。
键盘操作可达，按钮有明确中文名称。窄屏允许卡片和长 JSON 换行，不产生页面横向溢出。

## 验证约定

通过组件/应用流程测试、TypeScript 和生产构建；浏览器覆盖新目标、补参、刷新、网络状态、键盘和窄屏。
模型输出不使用 v-html。图片由 AssetGallery 管理，失败不能伪装成成功图像。
