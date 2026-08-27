# Architecture

Mini-Agent 是纯本地单用户系统。浏览器和遗留 TUI 是客户端，本机 backend 是唯一服务端；仓库不包含账户服务、Cloud、同步或 PostgreSQL 部署层。

## 部署边界

```text
frontend/ ── HTTP/SSE ──> backend (127.0.0.1:8000)
tui/      ── shared Python packages ──┘
                                  ├─ local model providers
                                  ├─ tools / Skills / MCP / Sandbox
                                  └─ ~/.mini_agent
```

- `frontend/` 不导入 Python，只调用本地 backend API。
- `backend/` 承载 Agent Runtime、模型 Provider、工具、项目、会话、设置和本地持久化。
- `tui/` 复用同一份 `ClientPaths`、配置、Provider 和会话存储。
- `domain` 不依赖外层；runtime 依赖 planner/tool 端口；provider 不依赖 storage；frontend 不绕过 API。

## 前端入口

`/` 直接渲染 `AgentApp` 和 Chat。认证页面、AuthProvider、账户状态和同步页面不存在；未知前端路径回到 `/`。Ant Design 的 `ConfigProvider` 与 `App` 外壳保留。

## 本地 API 与 Origin 防护

所有 Chat、Turn、项目和设置 API 都不要求 Cookie 或 Bearer token。已删除的 `/api/auth/*` 与 `/api/sync/*` 返回 404。

浏览器写请求仍有独立安全边界：带 `Origin` 的非只读请求必须匹配配置的 loopback Origin，否则返回 403；无 `Origin` 的本地 CLI 请求可以使用。CORS 的 `allow_credentials` 为 false。

## Runtime

`AgentRuntime` 分为三部分：

- `RuntimeState`：可序列化消息、模型设置、活动 Run、工具进度和完成摘要。
- `RuntimeServices`：planner、工具、持久化、审批、steering、Subagent、时钟和 ID 生成器；密钥与 callable 不序列化。
- `RuntimeExchange`：一次瞬态模型操作的请求、响应、流和推理回调。

每个 session 同时最多一个活动 Turn。稳定状态迁移会写入 SQLite；SSE 断线只取消订阅，不取消后台 worker。Job registry 按 system/session/thread/run/task 组织，不含账户 owner。

## 工具、Skills、MCP 与 Sandbox

- 工具参数始终按 schema 校验，并保持 workspace 边界和审批策略。
- 全局 Skills 位于 `~/.mini_agent/skills/`；项目 Skills 位于 `<workspace>/.mini_agent/skills/`，按项目和树哈希审批。
- 全局 MCP 位于 `~/.mini_agent/mcp/`；项目 MCP 输出仍视为不可信。
- Subagent 只允许单层 delegation。
- 严格 Sandbox Broker 未就绪时不得降级为普通进程。
- Sandbox 内部 `user_id` 只是 Broker 账户池/资源隔离键，不表示应用登录身份。

## 本地持久化

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

`config.toml` 仅保存非敏感本地设置。`runtime/state.db` 保存 Provider 元数据和密文 API Key；安装级密钥由 OS credential vault 管理。`runtime/projects.db` 保存项目索引；各 session 目录保存自己的 `state.db`、workspace 和 uploads。

新实现不扫描、不导入、不迁移旧 UUID 用户目录、`user.db`、认证缓存、同步数据库或旧密文。

## 数据流

```text
Browser request
  -> Origin middleware
  -> FastAPI route
  -> local application/session service
  -> AgentRuntime
  -> planner/provider/tools
  -> RuntimeEvent + SQLite checkpoint
  -> SSE projection
```

Provider wire formats只存在于 `backend/src/providers/`。Runtime 发布事件，UI 展示逻辑留在前端；本地 SQLite 不承担远端 revision、outbox 或同步投影。
