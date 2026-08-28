# Mini-Agent

Mini-Agent 是纯本地、单用户的桌面 Agent 应用。React/Vite 前端通过 loopback HTTP/SSE 访问本机 FastAPI backend；对话运行时、模型 Provider、工具、Skills、MCP、Sandbox、项目、会话与设置均在本机运行和保存。

项目不包含账户、登录、设备授权、云同步、Cloud 服务或 PostgreSQL 部署层。

## 工作区组成

| 目录 | 包 | 用途 |
| --- | --- | --- |
| [`backend/`](backend/README.md) | `mini-agent-backend` | FastAPI、本地 Agent Runtime、Provider、工具、MCP、Sandbox、Redis mailbox 与 SQLite/TOML 持久化 |
| [`frontend/`](frontend/README.md) | `mini-agent-web` | React/Vite/TypeScript 客户端，包括 Chat、项目、设置和 Benchmark 界面 |
| [`benchmarks/`](benchmarks/README.md) | benchmark harness | 9 个改编自开源基准的确定性任务、执行器与评分器 |

```text
frontend/ ── HTTP/SSE ──> backend (127.0.0.1:8000)
                              ├─ runtime / planning / providers / tools
                              ├─ Skills / MCP / Sandbox
                              ├─ ~/.mini_agent (TOML / SQLite / workspace)
                              └─ Redis (queued messages / active Turn mailbox)
```

开发时 Vite 把 `/api` 和 `/benchmark` 代理到 backend；生产本地模式由 backend 直接托管 `frontend/dist`。

## 环境要求

- Python 3.11+
- Node.js 20+
- [`uv`](https://docs.astral.sh/uv/)
- Docker Desktop（只用于仓库内 Redis 7.4）
- Windows 开发环境使用 Conda `dev` 环境

## 安装与启动

从仓库根目录安装 Python workspace 和前端依赖：

```powershell
conda activate dev
uv sync
cd frontend
npm ci
cd ..
docker compose up -d redis
```

启动 backend：

```powershell
uv run python -m backend.api
```

另开终端启动 Vite：

```powershell
conda activate dev
cd frontend
npm run dev
```

浏览器打开 <http://127.0.0.1:5173>。`/api/health` 只表示 backend 进程存活；`/api/ready` 会同时检查 SQLite 与 Redis，Redis 不可用时返回 503。

生产本地模式先构建前端，再启动 backend：

```powershell
cd frontend
npm run build
cd ..
uv run python -m backend.api
```

## 配置与本地数据

可参考 [`config.toml.example`](config.toml.example) 创建 `~/.mini_agent/config.toml`。Mini-Agent 使用以下顶层结构：

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

- `config.toml` 只保存非敏感的 Profile、Agent、Runtime、Sandbox、MCP 和 Skill 配置。
- `runtime/state.db` 保存 Provider 元数据和加密后的 API Key。
- `runtime/projects.db` 保存项目索引。
- `runtime/<session_id>/` 保存会话数据库、workspace 和 uploads。
- Redis 保存浏览器待发送草稿、active Turn steering mailbox 和幂等 receipt；未完成 delivery 的 receipt 不过期，acknowledged/returned receipt 保留 7 天，正式聊天历史仍以 SQLite 为权威。
- Redis 使用 `compose.yaml` 的命名卷和 AOF（`appendfsync everysec`）。待发送消息是明文，只允许绑定 `127.0.0.1:6379`。
- `MINI_AGENT_REDIS_URL` 默认是 `redis://127.0.0.1:6379/0`，属于部署环境变量，不写入 `config.toml`。
- Provider API Key 使用 OS credential vault 中的安装级密钥加密，不写入 TOML，也不通过 API 回显。

项目不会读取或迁移旧 UUID 用户目录、`user.db`、认证缓存、同步数据或旧密文。

Redis 不可用时不会回退到浏览器或进程内队列：历史读取仍可用，但 Turn 创建/恢复、queued-message CRUD 和 steering 会被阻断；运行中的 Turn 在最近安全边界以 `message_queue_unavailable` 失败。

## 安全边界

- 浏览器写请求只允许配置的 loopback Origin；无 `Origin` 的本地 CLI 请求允许执行。
- CORS 不启用 credentials，前端不发送登录 Cookie 或 Bearer 凭据。
- 可用逗号分隔的 `MINI_AGENT_ALLOWED_ORIGINS` 配置额外的精确本地来源；不要使用通配 Origin。
- 工具参数经过 schema、审批与 workspace 边界检查；项目 Skills 和外部 MCP 输出按不可信输入处理。
- 严格 Sandbox Broker 未就绪时不会静默降级为普通进程。

## 开发验证

```powershell
conda activate dev
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q

cd frontend
npm run typecheck
npm test
npm run build
```

真实浏览器 Turn 流程使用真实 backend、Vite 和本地假模型服务：

```powershell
cd frontend
npm run test:e2e
```

HTTP、Provider 和 E2E 测试不会调用付费模型 API。Windows 若 pytest 临时目录 ACL 阻止创建目录，请指定一个当前用户可写、此前不存在的唯一 `--basetemp`。

更多设计约束见 [`docs/architecture.md`](docs/architecture.md) 与 [`docs/development.md`](docs/development.md)。
