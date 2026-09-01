# 右侧工作面板

该目录实现主聊天之外的侧聊与终端窗口，并把窗口切换、尺寸和关闭动作交给上层应用协调。

- `RightPanel.tsx`：读取 `RightPanelPayload`，渲染窗口 Tabs、空状态与侧聊 `ChatPage`；负责创建、激活、关闭窗口和面板折叠，但不自行持久化会话数据。
- `TerminalPane.tsx`：连接终端流 API、装配 xterm 与 FitAddon，转发输入和尺寸变化，并在断开或能力不可用时展示状态。
- `RightPanel.test.tsx`：覆盖窗口切换、关闭、空状态以及侧聊/终端分派行为。

目录对外主要暴露默认组件 `RightPanel`。它依赖 `api/rightPanel` 的窗口 API、共享 `RightPanelPayload`/`Conversation` 类型以及聊天页；侧聊运行、队列和 rewind/reload 回调由 `AgentShell`/`AgentApp` 注入。`TerminalPane` 只处理单个 terminal window，不访问会话列表或应用全局状态。
