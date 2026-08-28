# 页面入口

该目录保存路由级页面。

- `ChatPage.tsx`：兼容入口，转发到 `pages/chat/ChatPage`。
- `BenchmarkPage.tsx`：任务选择、运行和结果展示。
- `TrashPage.tsx`：归档会话与已移除项目的恢复视图。
- 相应 `*.test.tsx` 覆盖页面行为。

页面通过 API 或 `AgentShell` 注入的命令工作；复杂聊天职责进一步归入 `chat/`。
