# 数学渲染适配

该包提供 Markdown 数学内容所需的 MathJax 入口与类型声明。

- `index.ts`：加载/配置数学渲染能力。
- `mathjax.d.ts`：补充第三方模块类型。

消费者是 `../components/MarkdownContent.tsx`；这里不保存页面状态。
