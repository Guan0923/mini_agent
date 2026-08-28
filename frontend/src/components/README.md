# 共享组件

该目录放置跨页面复用或拥有独立交互契约的组件。

- `AppSidebar.tsx`：侧边栏组合门面；细节位于 `sidebar/`。
- `UserSettingsModal.tsx`：设置弹窗门面；状态与分区位于 `settings/`。
- `MarkdownContent.tsx`、`latexClipboard.ts`：Markdown/数学渲染和复制处理。
- `DecisionCard.tsx`：审批、问答与 Plan Review 决策 UI。
- `AssistantIcon.tsx`、`IconAction.tsx`、`ShimmerText.tsx`：基础展示原语。
- `OceanScene.tsx` 与 `beach/`：空状态海景及太阳位置计算。

组件依赖 Ant Design 和共享类型，不直接复制 API 错误分类或持久化逻辑；相应 `*.test.ts(x)` 覆盖公开交互。
