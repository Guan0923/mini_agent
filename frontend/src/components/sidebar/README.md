# 侧边栏领域组件

该包拆分会话历史、项目设置和个人资料交互，由 `../AppSidebar.tsx` 组合。

- `ConversationHistory.tsx`：`HistoryRow`、重命名/归档/删除菜单和公开的 `confirmDelete`。
- `ProjectSettings.tsx`：项目改名、路径选择、Skill 信任撤销和移除入口。
- `ProfileSection.tsx`：`ProfilePopover`、`ProfileLabel` 与个人资料校验。
- `useHorizontalOverflow.ts`：共享 ResizeObserver 溢出测量。
- `useProjectExpansion.ts`：项目 Collapse 展开状态及 localStorage 持久化。
- `types.ts`：`AppSidebarProps` 与历史变更回调契约。

该包只发出回调，不直接调用项目/会话 API；CSS class 由 styles 的 sidebar 文件定义。
