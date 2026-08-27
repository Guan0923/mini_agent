# Mini-Agent Web

React/Vite/TypeScript 本地客户端，包含 Chat、项目与 Benchmark。访问 `/` 直接进入 Chat；没有登录、注册、找回密码、设备授权或云同步页面。

## 启动

先从仓库根目录启动 backend：

```powershell
conda activate dev
uv sync
uv run python -m backend.api
```

再启动 Vite：

```powershell
cd frontend
npm ci
npm run dev
```

打开 <http://127.0.0.1:5173>。Vite 将 `/api` 与 `/benchmark` 代理到本机 8000 端口。

生产本地模式由 backend 托管构建产物：

```powershell
cd frontend
npm run build
cd ..
uv run python -m backend.api
```

## 请求安全

前端不发送 Cookie 或 Bearer 登录凭据。带 Origin 的写请求必须来自 backend 配置的 loopback 来源；CORS 不启用 credentials。开发默认允许 `localhost:5173` 和 `127.0.0.1:5173`。

## 设置

本地 Profile、Agent、Runtime、Sandbox 和 Provider 设置通过 `/api/settings/*` 保存。API Key 不写入 TOML，也不会在响应中回显；backend 将其加密保存在 `~/.mini_agent/runtime/state.db`。

## 结构

```text
src/App.tsx                    Ant Design 应用外壳
src/app/AgentApp.tsx           本地应用状态与路由装配
src/pages/chat/                Chat 页面和运行时消息
src/pages/BenchmarkPage.tsx    Benchmark 页面
src/components/                Sidebar、设置和共享组件
src/api/                       本地 backend API 客户端
e2e/turn-flow.spec.ts          真实 backend + 假模型 Turn 流程
```

## 验证

```powershell
npm run typecheck
npm test -- --run
npm run build
```
