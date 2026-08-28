# Mini-Agent Web

`mini-agent-web` 是 Mini-Agent 的 React 18、Vite 5、TypeScript 本地客户端。它只通过 HTTP/SSE 调用本机 backend，不直接访问 Python 实现或本地数据库。

应用包含 Chat、Turn 运行状态、项目与对话管理、本地设置、文件引用和 Benchmark 页面；访问 `/` 直接进入 Chat，不包含登录、注册、设备授权或云同步页面。

## 环境与启动

先在仓库根目录启动 Redis 与 backend：

```powershell
conda activate dev
uv sync
docker compose up -d redis
uv run python -m backend.api
```

再启动前端开发服务器：

```powershell
cd frontend
npm ci
npm run dev
```

打开 <http://127.0.0.1:5173>。Vite 默认把 `/api` 和 `/benchmark` 代理到 <http://127.0.0.1:8000>；需要其他本地 backend 时设置 `MINI_AGENT_BACKEND_URL`。

## 可用脚本

| 命令 | 用途 |
| --- | --- |
| `npm run dev` | 启动 Vite 开发服务器 |
| `npm run build` | 生成 `dist/` 生产构建 |
| `npm run preview` | 本地预览构建产物 |
| `npm run typecheck` | 运行 TypeScript `--noEmit` 检查 |
| `npm test` | 运行 Vitest 单元/组件测试 |
| `npm run test:e2e` | 运行 Playwright 真实 backend + Vite + 假模型流程 |

生产本地模式由 backend 托管构建产物：

```powershell
cd frontend
npm run build
cd ..
uv run python -m backend.api
```

## 源码结构

```text
src/
├─ App.tsx                  Ant Design 应用入口
├─ app/                     应用装配、路由、主题、Turn 控制与状态投影
├─ api/                     HTTP/SSE 客户端与请求类型
├─ commands/                Composer 命令及补全
├─ components/              Sidebar、设置、Markdown 和共享组件
├─ pages/chat/              Chat、时间线、Composer、文件引用与待发送消息
├─ pages/BenchmarkPage.tsx  Benchmark 界面
├─ math/                    数学公式支持
└─ styles/                  样式资源

e2e/                        Playwright Turn 端到端测试
```

请求、错误分类和持久化逻辑应留在现有 API/应用层，不在页面组件中重复实现。Runtime 事件由 backend 发布，展示与折叠逻辑留在前端。

## 请求与数据安全

- 前端不发送登录 Cookie 或 Bearer 凭据。
- 带 `Origin` 的写请求必须来自 backend 配置的 loopback 来源。
- 开发默认允许 `http://localhost:5173` 与 `http://127.0.0.1:5173`，CORS 不启用 credentials。
- Provider API Key 不写入浏览器存储、不在响应中回显；backend 加密后保存到 `~/.mini_agent/runtime/state.db`。
- 待发送消息不写入 localStorage；前端通过 queued-message API 读取 Redis 权威状态，POST 成功后才清空 Composer。

## 验证

```powershell
npm run typecheck
npm test
npm run build
npm run test:e2e
```

前三项是前端静态、单元/组件和生产构建验证；`test:e2e` 会启动真实本地 backend 与 Vite，但模型响应来自本地假服务，不产生模型 API 费用。
