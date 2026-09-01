# 前端共享契约

该目录保存 API、应用协调层与页面共同使用的 TypeScript 数据契约。这里只定义类型，不执行请求、持久化或 UI 逻辑。

- `app.ts`：页面、运行模式、权限、展示模式、思考配置以及本地 Profile、Tool、Skill 摘要。
- `files.ts`：项目文件和上传文件的 `FileReference`、`SessionFileInfo` 契约。
- `runtime.ts`：Turn/Item/Frame、Trace、Agent Thread 流事件、Todo、ToolEvent 和运行模型配置。
- `conversations.ts`：`Conversation`、`ChatMessage`、`SidebarThread` 与消息指标；通过类型导入组合文件、决策和 Runtime 契约。
- `decisions.ts`：工具审批、Plan Review、问题和 Skill 信任使用的 `DecisionRequest` 族。
- `rightPanel.ts`：右侧面板状态、窗口和能力响应。
- `benchmarks.ts`：Benchmark 任务、结果和 trace 事件。
- `index.ts`：兼容现有 `../types` 类型导入的稳定门面；业务模块在需要缩小依赖时可以直接导入具体文件。

本包只依赖同目录内更基础的类型文件。`runtime.ts` 依赖 `app.ts` 的模式类型，`conversations.ts` 依赖 `runtime.ts`、`files.ts` 和 `decisions.ts`；其余文件相互独立，不能反向依赖 API、App 或组件。
