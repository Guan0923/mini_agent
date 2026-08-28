# 前端源码

前端是 React/Vite 单页应用，通过 loopback HTTP/SSE 调用本地 backend。

- `api/`：协议和请求边界。
- `app/`：应用状态、运行控制与布局装配。
- `pages/`：路由级页面，聊天细节位于 `pages/chat/`。
- `components/`：共享和领域组件。
- `commands/`、`math/`、`styles/`：Composer 命令、数学渲染和样式层。
- `types.ts`：前端共享协议类型；`main.tsx`/`App.tsx` 是启动入口。
- `api.ts`：保持 `./api` 根导入稳定并转发 `api/index.ts`；`styles.css` 装配 `styles/index.css`。
- `katex-shim.ts`：为 texmath 提供拒绝 KaTeX 渲染的 MathJax 适配边界；`markdown-it-texmath.d.ts` 与 `turndown-plugin-gfm.d.ts` 补充第三方类型。
- `vite-env.d.ts`：声明 Vite 环境类型；`test-setup.ts` 安装 Vitest DOM matcher 和浏览器 API 测试桩。

业务组件不直接持久化数据或复制 transport；公共协议变更必须同步 API、Runtime 投影和测试。
