# 应用协调层

该包连接 API、浏览器持久化、运行控制器与页面壳层。

- `AgentApp.tsx`：装配全局状态、当前会话和 `AgentShell`，将加载、队列与 Sandbox run 生命周期委托给专用模块。
- `conversationHydration.ts`：`hydrateConversationCatalog` 并发读取 Session/Project 索引，合并浏览器缓存，过滤已删除或归属项目的 stale conversation。
- `useQueuedMessages.ts`：按 conversation 管理 queued message map，提供刷新和函数式更新接口。
- `useSandboxRunLifecycle.ts`：在 run 前检查 Broker 健康，并统一连接 run controller 的 start/stop/recover 状态。
- `AgentShell.tsx`：桌面/移动布局与页面路由投影。
- `conversationActions.ts`、`projectActions.ts`：会话和项目命令集合。
- `conversationProjection.ts`：`withLoadedTurns` 把 Runtime 节点投影为当前消息路径。
- `runController.ts`：`createRunController` 管理 SSE、AbortController 和运行恢复。
- `storage.ts`、`sessionModes.ts`、`queuedMessages.ts`：浏览器缓存的独立契约。
- `displayMode.ts`、`theme.ts`、`routes.tsx`、`types.ts`：展示模式、主题、路由与应用内部类型。
- `runtime/`：Runtime 节点归一化、reduce 与消息投影。

应用层可依赖 API 和共享类型；页面组件通过 props 使用这里的控制器，不直接持有全局可变单例。
