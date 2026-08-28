# 样式分层

`index.css` 按固定顺序装配全局样式，顺序即层叠契约。

- `foundation.css`：变量、reset 和应用骨架。
- `chat-messages.css`、`chat-runtime.css`、`chat-markdown.css`、`chat-todo.css`：消息、Runtime Item、Markdown/数学与 Todo。
- `composer-core.css`、`composer-decisions.css`、`composer-completion.css`、`composer-files.css`：Composer、决策、补全和文件上传。
- `pages.css`、`chat-motion.css`：页面布局与动画。
- `overrides-sidebar.css`、`overrides-runtime.css`、`overrides-pages.css`：按领域覆盖 Ant Design 默认样式。
- `responsive.css`：响应式收口，必须最后加载。

新增选择器应进入对应领域文件；不得重新建立单一超大 overrides 文件。
