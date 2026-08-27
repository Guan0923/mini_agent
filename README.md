# Mini-Agent

Mini-Agent 是纯本地单用户 Agent 应用。React/Vite 前端通过 loopback HTTP/SSE 访问本机 FastAPI backend；Agent Runtime、模型调用、工具、项目、会话与设置都保存在本机。应用没有账户、游客、登录 Cookie、设备授权、云同步、Cloud 服务或 PostgreSQL。

当前客户端方向是 Web；`tui/` 是仍可运行的遗留 Textual 入口，并与 Web 共用本地配置和运行数据。

## 架构

```text
React/Vite frontend ── HTTP/SSE ──> FastAPI backend (127.0.0.1:8000)
                                      ├─ Runtime / planners / providers
                                      ├─ tools / Skills / MCP / Sandbox
                                      └─ local TOML / SQLite / workspace
```

- 浏览器访问 `/` 直接进入 Chat；其他前端路径统一回到 `/`。
- `frontend/` 只调用 backend API。开发时 Vite 代理 `/api` 和 `/benchmark`，生产时 backend 可托管 `frontend/dist`。
- `backend/` 负责本地 API、Agent Runtime、Provider、工具、项目、MCP、Sandbox 和持久化。
- `tui/` 直接复用 backend/domain/runtime，是兼容入口，不扩展新的 Web 功能。

## 快速开始

需要 Python 3.11+、Node.js 20+ 和 `uv`：

```powershell
uv sync
cd frontend
npm ci
cd ..
uv run python -m backend.api
```

另开终端：

```powershell
cd frontend
npm run dev
```

浏览器打开 <http://127.0.0.1:5173>。

生产本地模式：

```powershell
cd frontend
npm run build
cd ..
uv run python -m backend.api
```

## 本地数据

Mini-Agent 只使用以下 `~/.mini_agent` 顶层结构，不读取或迁移旧 UUID 用户目录：

```text
~/.mini_agent/
├─ mcp/
├─ plugins/
├─ runtime/
│  ├─ state.db
│  ├─ projects.db
│  └─ <session_id>/
├─ skills/
└─ config.toml
```

- `config.toml` 保存 Profile、Agent、Runtime、Sandbox 和能力开关等非敏感配置。
- `runtime/state.db` 保存 Provider 集合、激活状态和加密 API Key。
- `runtime/projects.db` 保存项目索引。
- `runtime/<session_id>/` 保存会话数据库、workspace 和 uploads。
- Provider API Key 使用 OS credential vault 中的安装级固定密钥加密；TOML 和 API 响应不包含明文密钥。

旧 `~/.mini_agent/<uuid>/`、`user.db`、同步数据和旧密文不会自动迁移。需要保留旧数据时请在升级前自行备份。

## 浏览器安全边界

登录移除后仍保留 loopback Origin 防护：浏览器写请求只允许配置的本地 Origin；无 `Origin` 的本地 CLI 请求允许执行；CORS 不启用凭据。可通过环境变量设置精确来源：

```powershell
$env:MINI_AGENT_ALLOWED_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
```

## 本地设置 API

- `GET /api/settings`
- `GET/PUT /api/settings/profile`
- `PUT /api/settings/agent`
- `PUT /api/settings/runtime`
- `PUT /api/settings/sandbox`
- `/api/settings/providers` 下的增删改、激活和模型发现接口

`/api/auth/*` 和 `/api/sync/*` 不存在。

## 遗留 TUI

```powershell
uv run python run.py --planner rule "calculate (18 + 6) * 4"
uv run python -m tui
uv run mini-agent --resume <session_id>
```

## 开发与验证

```powershell
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q
cd frontend
npm run typecheck
npm test -- --run
npm run build
```

HTTP/provider 测试使用本地假服务，不调用付费模型 API。Windows 若 pytest 临时目录 ACL 阻止创建目录，请使用当前用户有权限的唯一 `--basetemp`。

详见 [docs/architecture.md](docs/architecture.md)、[docs/development.md](docs/development.md) 和 [frontend/README.md](frontend/README.md)。
