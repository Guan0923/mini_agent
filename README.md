# Mini-Agent

Mini-Agent 是一个 local-first Agent 应用：Agent Runtime、模型调用、工具执行、会话历史和工作区在用户电脑上的 backend 中运行；浏览器前端通过 loopback HTTP/SSE 访问它。账户、邮件验证和加密云同步由可选的 cloud 服务提供，只有 cloud 访问 PostgreSQL。

当前客户端方向是 Web 前端；`tui/` 是仍可运行的遗留 Textual 入口，不再承载新功能。

## 架构

```text
React/Vite frontend ── HTTP/SSE ──> FastAPI backend (127.0.0.1:8000)
                                      ├─ Runtime / planners / providers
                                      ├─ tools / Skills / MCP / Sandbox
                                      ├─ per-user SQLite / sync outbox
                                      └─ optional HTTPS ──> cloud (8100) ──> PostgreSQL
```

- `frontend/` 只调用 backend API；开发时 Vite 将 `/api` 和 `/benchmark` 代理到 8000，生产时 backend 可托管 `frontend/dist`。
- `backend/` 负责 FastAPI、Agent Runtime、会话、模型 Provider、工具、项目、RAG、MCP、Sandbox、审计和同步；不连接 PostgreSQL，不负责 SMTP 或 cloud 主密钥。
- `cloud/` 负责账户、密码/验证码、设备授权、密钥封装、加密快照和 PostgreSQL。
- `tui/` 直接复用 backend/domain/runtime 包，是兼容性入口，不是 Web API 替代实现。
- `benchmarks/`、`docs/`、`scripts/` 是支持目录。

## 能力

Web 对话、流式事件、历史/分支/恢复、消息队列、取消运行、Plan mode、用户澄清、Plan Review、工具审批、Full access、workspace-confined 文件/搜索/网页/命令工具、文件引用与上传、RAG、Provider 设置、项目 Skills 信任、单层 Subagents、只读 stdio MCP、审批型外部 MCP、Windows Sandbox Broker、本地 SQLite 和加密增量同步均已纳入当前架构。Broker 未就绪时严格沙箱不会自动降级为普通进程。

## 快速开始

需要 Python 3.11+、Node.js 20+ 和 `uv`；cloud/PostgreSQL/Qdrant 仅在对应功能需要时使用。

```powershell
uv sync
cd frontend
npm ci
cd ..
uv run python -m backend.api
```

另开终端启动前端：

```powershell
cd frontend
npm run dev
```

浏览器打开 <http://127.0.0.1:5173>。只运行本地游客/离线 Agent 不需要 Docker 或 cloud。若不使用 `uv`，可执行 `python -m pip install -e "backend[sync]" -e tui`。

### 可选 cloud、PostgreSQL 和 Qdrant

```powershell
$env:MINI_AGENT_SECRET_KEY = "replace-with-a-32-byte-development-secret"
docker compose up -d postgres cloud
$env:CLOUD_URL = "http://127.0.0.1:8100"
docker compose up -d qdrant  # 仅在需要向量检索时
```

`DATABASE_URL`、`MINI_AGENT_SECRET_KEY` 和 SMTP 只属于 cloud 进程/部署密钥，不要写入 frontend 或本地用户 TOML。

### 生产本地模式

```powershell
cd frontend
npm run build
cd ..
uv run python -m backend.api
```

backend 发现 `frontend/dist` 后会托管它。需要 HTTPS 或非默认来源时，在用户配置的 `[web]` 中设置精确的 `public_url`、`allowed_origins` 和 `cookie_secure`。

## 遗留 TUI

```powershell
uv run python run.py --planner rule "calculate (18 + 6) * 4"
uv run python -m tui
uv run mini-agent --resume <session_id>
```

TUI 配置位于 `~/.mini_agent-cache/tui/config.toml`。Web 用户的 Provider、加密 API Key 和同步状态属于每个用户的 `user.db`，不会从 TUI 配置导入。

## 本地数据与安全边界

| 数据 | 位置 | 说明 |
| --- | --- | --- |
| 浏览器会话缓存 | `~/.mini_agent-cache/auth/client.db` | 哈希后的本地会话和身份缓存 |
| 用户偏好 | `~/.mini_agent/<user_id>/config.toml` | 非敏感设置、能力开关和项目 Skill 信任 |
| Provider/同步状态 | `~/.mini_agent/<user_id>/user.db` | 加密 API Key、cloud token、同步事务 |
| 会话运行时 | `~/.mini_agent/<user_id>/runtime/<session_id>/` | `state.db`、`workspace/`、`uploads/` |
| 脱敏日志 | `~/.mini_agent-cache/logs/<user_id>/` | JSONL 诊断与事件 |

项目 `.mini_agent/skills/` 是不可信内容，项目 Skill 必须逐个审批。模型参数、网页结果和外部 MCP 输出都要经过 schema、路径边界和审批。不要提交 API Key、Cookie、认证头、sync token、SMTP 密码、cloud 主密钥或真实用户数据。

## 目录结构

```text
backend/src/domain/          provider-neutral domain
backend/src/planning/        rule/LLM planner
backend/src/runtime/         application/conversation/execution/Plan/recovery
backend/src/providers/       generic transport and provider adapters
backend/src/tools/           ToolRegistry and grouped tools
backend/src/mcp/             stdio server and external MCP client
backend/src/storage/         local SQLite and user settings
backend/src/sync/            encrypted snapshot sync/outbox
backend/src/observability/   redaction and JSONL events
frontend/                    React/Vite/TypeScript client
cloud/src/cloud/             account and PostgreSQL service
tui/src/                     deprecated Textual client
benchmarks/                  benchmark tasks and graders
docs/                        architecture and development notes
tests/                       focused Python tests
```

依赖方向保持向内：domain 不依赖外层；provider 不导入 storage；frontend 不导入 Python；PostgreSQL 只由 cloud 访问。详见 [docs/architecture.md](docs/architecture.md)、[docs/development.md](docs/development.md) 和 [frontend/README.md](frontend/README.md)。

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

PostgreSQL 集成测试使用独立测试数据库。Windows 若 pytest 临时目录 ACL 阻止创建目录，请使用当前用户有权限的唯一 `--basetemp`，不要删除其他任务的临时目录。

## 入口

- Backend：`python -m backend.api`，默认 `127.0.0.1:8000`
- Frontend：`cd frontend; npm run dev`，默认 `127.0.0.1:5173`
- Cloud：`python -m cloud` 或 Compose，默认 `127.0.0.1:8100`
- Read-only MCP：`mini-agent-mcp --workspace <path>`
