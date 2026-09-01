# 聊天页面

该包实现 Composer、Turn 消息投影和同 Turn steering/rewind 交互。

- `ChatPage.tsx`：页面级组合、slash command、Turn 提交和主要视图渲染；保留 `composerAction`、`CHAT_COMPACT_WIDTH` 的稳定导出。
- `ChatToolbar.tsx`：Chat/Trace 视图切换和主 Thread/Subagent Thread 选择工具栏。
- `useResponsiveChatLayout.ts`：通过 Ant Design breakpoint 和容器 `ResizeObserver` 统一计算移动端/紧凑布局。
- `useChatScroll.ts`：维护 24px 底部阈值、对话切换时的底部锚定、内容尺寸变化跟随和手动回到底部操作。
- `useChatCommands.ts`：封装 slash command 补全，以及 `/trace`、`/help`、`/new`、`/skills`、`/compact` 的控制流和 compact pending 清理。
- `useQueuedMessageFlow.ts`：封装 queue create/update/delete、运行中 steering、终态自动 FIFO flush、delivery acknowledgement 刷新和编辑回填。
- `contracts.ts`：`ChatPageProps`、`ChatRunRequest`、`PendingUpload` 和 `composerAction`。
- `useRuntimeControls.ts`：running Turn 的 mode/permission/reasoning 热更新。
- `useComposerFiles.ts`：`@` 引用搜索、上传、重试、删除与会话切换清理。
- `useMessageEditing.ts`：用户消息编辑、rewind、fork 和版本切换。
- `ChatMessageList.tsx`、`messageParts.tsx`：消息列表、Assistant Runtime Item 与操作按钮。
- `Composer.tsx`、`FileMentionEditor.tsx`、`QueuedMessageList.tsx`：输入、补全、上传和 FIFO 队列 UI。
- `ConversationTimeline.tsx`、`TimelineTicks.module.css`：长对话导航。
- `todoPanel.tsx`：从最新 todo Item 投影任务列表。

API/SSE 由 `api/` 和 `app/runController` 提供；本包必须保持 pause/steer、rewind 和 `Message[][]` 协议语义。
