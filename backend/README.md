# Mini-Agent Backend

`mini-agent-backend` 是 Mini-Agent 的 Python 3.11+ 本地服务包，提供 loopback FastAPI、Agent Runtime、模型 Provider、工具、Skills、MCP、Sandbox 以及 TOML/SQLite 持久化。

它面向单机单用户运行，不包含账户、登录、Cloud、同步或 PostgreSQL 服务。

## 安装与启动

backend 是根 `uv` workspace 的成员。请从仓库根目录运行：

```powershell
conda activate dev
uv sync
uv run python -m backend.api
```

服务监听 `127.0.0.1:8000`：

- `GET /api/health`：进程健康检查
- `GET /api/ready`：本地设置和项目数据库就绪检查
- `/docs`：FastAPI OpenAPI 界面

如果 `frontend/dist` 存在，backend 会在 `/` 托管该构建；可通过 `MINI_AGENT_FRONTEND_DIST` 指定其他构建目录。

## 包结构

源码使用 flat layout：`backend/src` 本身映射为 Python 包 `backend`。

```text
src/
├─ api/            FastAPI 装配、Origin 防护、HTTP/SSE 路由
├─ domain/         无外层依赖的消息、计划、会话、Skill 与运行状态
├─ jobs/           本地后台 Job 注册和生命周期
├─ mcp/            只读 stdio MCP server 与审批型外部 MCP 客户端
├─ observability/  日志、指标与递归脱敏
├─ planning/       Planner、上下文与模型请求生命周期
├─ providers/      通用 transport 和 Provider 适配
├─ runtime/        Agent 装配、会话、执行、Plan mode、恢复与事件
├─ sandbox/        Sandbox Launcher、Broker 客户端与本地服务
├─ skills/         Skill 发现、激活与信任
├─ storage/        本地 TOML、SQLite 与凭据加密
└─ tools/          ToolRegistry、schema、审批和工具实现
```

依赖方向保持向内：`domain` 不依赖外层，Provider 不导入 storage，Runtime 发布事件而不包含前端展示逻辑。Sandbox Broker 不可用时不得静默降级。

## API 分组

| 路径 | 用途 |
| --- | --- |
| `/api/settings` | Profile、Agent、Runtime、Sandbox 与 Provider 设置 |
| `/api/projects` | 本地项目、会话与项目 Skill 信任 |
| `/api/sidebar-threads` | 对话列表、归档、恢复与删除 |
| `/api/turns` | Turn 创建、SSE、暂停、恢复、steering、rewind、fork 与 compact |
| `/api/sessions/{id}/files` | 会话文件上传、列表、读取与删除 |
| `/api/jobs` | 后台 Job 查询与取消 |
| `/api/sandbox` | Sandbox 状态、安装与修复 |
| `/benchmark` | 独立挂载的本地 benchmark 子应用 |

浏览器非只读请求会校验 `Origin`。默认只允许配置的 loopback 来源；`MINI_AGENT_ALLOWED_ORIGINS` 接受逗号分隔的精确 Origin。无 `Origin` 的本地 CLI 请求允许执行，CORS 不启用 credentials。

## 配置与持久化

默认数据根目录为 `~/.mini_agent`：

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

`config.toml` 只保存非敏感配置。Provider API Key 使用 OS credential vault 中的安装级密钥加密后写入 `runtime/state.db`。不要把密钥、Cookie、认证头或完整环境写入日志和测试输出。

## MCP stdio server

安装 workspace 依赖后可启动只读 MCP server，并把工具可见范围限制到指定目录：

```powershell
uv run mini-agent-mcp --workspace C:\path\to\workspace
```

省略 `--workspace` 时使用当前目录。传入路径必须是已存在的目录。

## 验证

从仓库根目录运行：

```powershell
conda activate dev
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q
```

HTTP 与 Provider 测试使用 mock 或本地假服务，不调用付费模型 API。涉及浏览器的真实流程由 `frontend` 包的 `npm run test:e2e` 覆盖。
