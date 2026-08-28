# Conversation API

该包实现会话与 Turn 的浏览器协议，保持 backend 的 Session/SidebarThread/Turn 边界。

- `chat.ts`：`streamChat`、`streamAttachedTurn`、`streamResume`、`streamRewind`、`pauseTurn`、`steerTurn` 及 SSE 帧解析。
- `sessions.ts`：Session 创建/列表/归档和 `getSessionNodes`、`patchRuntimeConfig`。
- `sidebarThreads.ts`：侧边栏 Thread 的创建、改名、归档、恢复、删除。
- `turns.ts`：Turn 列表、fork、compact 和 current-data 切换。
- `chat.test.ts`：真实 fetch/SSE 协议单元测试。

公开接口由 `index.ts` 汇总。该包依赖 `transport/` 与 `app/runtime` 的节点归一化，不依赖 React 组件。
