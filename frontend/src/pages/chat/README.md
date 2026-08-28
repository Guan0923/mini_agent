# 聊天页面

该包实现 Composer、Turn 消息投影和同 Turn steering/rewind 交互。

- `ChatPage.tsx`：页面级编排、命令执行、队列 flushing 与运行提交。
- `contracts.ts`：`ChatPageProps`、`ChatRunRequest`、`PendingUpload` 和 `composerAction`。
- `useRuntimeControls.ts`：running Turn 的 mode/permission/reasoning 热更新。
- `useComposerFiles.ts`：`@` 引用搜索、上传、重试、删除与会话切换清理。
- `useMessageEditing.ts`：用户消息编辑、rewind、fork 和版本切换。
- `ChatMessageList.tsx`、`messageParts.tsx`：消息列表、Assistant Runtime Item 与操作按钮。
- `Composer.tsx`、`FileMentionEditor.tsx`、`QueuedMessageList.tsx`：输入、补全、上传和 FIFO 队列 UI。
- `ConversationTimeline.tsx`、`TimelineTicks.module.css`：长对话导航。
- `todoPanel.tsx`：从最新 todo Item 投影任务列表。

API/SSE 由 `api/` 和 `app/runController` 提供；本包必须保持 pause/steer、rewind 和 `Message[][]` 协议语义。
