# 应用协调层

该包连接 API、浏览器持久化、运行控制器与页面壳层。

- `AgentApp.tsx`：装配全局状态、启动加载、当前会话和 `AgentShell`。
- `AgentShell.tsx`：桌面/移动布局与页面路由投影。
- `conversationActions.ts`、`projectActions.ts`：会话和项目命令集合。
- `conversationProjection.ts`：`withLoadedTurns` 把 Runtime 节点投影为当前消息路径。
- `runController.ts`：`createRunController` 管理 SSE、AbortController 和运行恢复。
- `storage.ts`、`sessionModes.ts`、`queuedMessages.ts`：浏览器缓存的独立契约。
- `displayMode.ts`、`theme.ts`、`routes.tsx`、`types.ts`：展示模式、主题、路由与应用内部类型。
- `runtime/`：Runtime 节点归一化、reduce 与消息投影。

应用层可依赖 API 和共享类型；页面组件通过 props 使用这里的控制器，不直接持有全局可变单例。
