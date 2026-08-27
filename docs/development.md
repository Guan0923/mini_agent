# Development

## 环境

从仓库根目录运行：

```powershell
conda activate dev
uv sync
cd frontend
npm ci
cd ..
```

启动本地 backend 与前端：

```powershell
uv run python -m backend.api
```

```powershell
cd frontend
npm run dev
```

不需要 Docker、PostgreSQL、SMTP、Cloud URL 或账户密钥。

## 配置和数据

首次运行会初始化 `~/.mini_agent` 的严格五项顶层：`mcp/`、`plugins/`、`runtime/`、`skills/` 和 `config.toml`。初始化不读取 `.env`，也不扫描旧 UUID 目录。

`config.toml` 保存 Profile、Agent、Runtime、Sandbox 与能力配置。Provider 元数据及加密 API Key 位于 `runtime/state.db`，项目索引位于 `runtime/projects.db`。Web 与 TUI 使用同一份路径契约。

不要实现旧 `user.db`、旧目录、旧密文、账户或同步数据迁移。用户旧数据的备份、移动和删除不属于应用启动流程。

## 浏览器开发边界

Vite 将 `/api` 与 `/benchmark` 代理到 `127.0.0.1:8000`。浏览器请求不携带登录凭据；写请求由 Origin 中间件保护。默认允许：

```text
http://localhost:5173
http://127.0.0.1:5173
```

需要其他本地来源时设置逗号分隔的 `MINI_AGENT_ALLOWED_ORIGINS`。不要使用通配 Origin。

## 代码边界

- `backend/src/domain/`：与 provider、存储和展示无关的领域对象。
- `backend/src/planning/`：planner、上下文和模型请求生命周期。
- `backend/src/runtime/`：装配、会话、执行、Plan mode、恢复和事件。
- `backend/src/providers/`：通用 transport 与 provider 适配。
- `backend/src/tools/`：工具 schema、实现、审批和路径边界。
- `backend/src/storage/`：本地 TOML/SQLite 和凭据加密。
- `frontend/`：React/Vite/TypeScript 本地客户端。
- `tui/`：遗留兼容入口。

不要为新 Web 功能扩展 TUI，不要让 provider 导入 storage，不要让 frontend 导入 backend 实现。

## 测试

```powershell
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q

cd frontend
npm run typecheck
npm test -- --run
npm run build
```

Playwright Turn 流程使用真实 backend、Vite 和本地假模型服务；不要调用付费模型 API。Provider transport 测试同样使用 mock 或本地假服务。

重点契约：

- 全新临时 Home 只创建严格五项顶层，不创建 UUID、sync、`user.db` 或认证缓存。
- 无 Cookie/Bearer 可访问 Chat、Turn、项目与设置 API。
- `/api/auth/*` 与 `/api/sync/*` 为 404。
- 外部浏览器 Origin 写请求为 403；允许的 loopback Origin 和无 Origin CLI 请求可写。
- Provider API 不回显密钥，SQLite 不含明文，重启后可由 OS vault 密钥解密。
- `/` 无登录探测和 Spinner，直接出现 Chat。

Windows pytest 若默认临时目录 ACL 失败，使用当前用户可写且此前不存在的唯一 `--basetemp`。完整测试出现资源问题时，应明确区分聚焦验证与完整验收。

## 安全

日志与错误必须递归脱敏。不得写入 API Key、认证头、Cookie、敏感 MCP 环境值或完整进程环境。项目 Skills 和外部 MCP 输出均视为不可信；Sandbox Broker 安全边界与登录移除无关，必须继续严格执行。
