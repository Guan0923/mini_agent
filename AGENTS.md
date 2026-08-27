# Mini-Agent 仓库协作规范

## 项目定位

Mini-Agent 是纯本地单用户 Agent 应用：浏览器前端通过 loopback HTTP/SSE 访问本机 backend；backend 承载 Runtime、模型调用、工具、会话、项目和本地设置。仓库不包含账户、登录、设备授权、云同步、Cloud 或 PostgreSQL 子系统。

以当前源码、测试、`pyproject.toml` 和 `frontend/package.json` 为准；文档与源码冲突时先核对实现。

## 协作边界

- 动手前用简短中文说明变更与验证范围。
- 先检查 `git status --short` 和相关文件，保留用户已有修改和未跟踪文件。
- 未经授权不得 reset、清理工作树、覆盖无关文件、创建 commit 或 push。
- 优先使用 `apply_patch` 做有意编辑，格式化后检查差异。
- 日志、配置和测试输出必须脱敏，不回显 API Key、Cookie、认证头或完整环境。
- 计划模式中的取舍由用户决定；先解释影响与选项，再使用用户输入工具。

## 架构与依赖

```text
frontend/ ── HTTP/SSE ──> backend (127.0.0.1:8000)
                              ├─ runtime / planning / providers / tools
                              ├─ local TOML / SQLite / MCP / Sandbox
                              └─ ~/.mini_agent
```

- `backend/src/domain/`：无外层依赖的消息、计划、会话、Skill 和运行状态。
- `backend/src/planning/`：planner、上下文和模型请求生命周期。
- `backend/src/runtime/`：装配、会话、执行、Plan mode、恢复和事件。
- `backend/src/providers/`：通用 transport 与 provider 适配；不得导入 storage。
- `backend/src/tools/`：ToolRegistry、schema 和工具实现。
- `backend/src/mcp/`：只读 server 与审批型外部 MCP 客户端。
- `backend/src/storage/`：本地 TOML、SQLite 和凭据加密。
- `frontend/`：React/Vite/TypeScript，只调用 API。

依赖向内；Runtime 发布事件，展示留在前端。Sandbox Broker 未就绪时不得静默降级。

## 本地数据契约

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

`config.toml` 只保存非敏感配置；Provider API Key 使用 OS credential vault 的安装级密钥加密后写入 `runtime/state.db`。Web 与 backend 使用同一份配置和运行数据。不得恢复旧 UUID、`user.db`、认证缓存、同步数据或旧密文迁移。

Sandbox 内部 `user_id` 仅是 Broker 资源隔离标识，不是应用登录身份。

## 常用命令

```powershell
conda activate dev
uv sync
cd frontend; npm ci; cd ..
uv run python -m backend.api
```

另开终端执行 `cd frontend; npm run dev`。生产本地模式先 build，再由 backend 托管 `frontend/dist`。

## 编码约束

- Python 使用四空格、类型标注、`snake_case` 与 `PascalCase`。
- React 遵循现有 API 层和组件模式，不在组件中复制请求、错误分类或持久化逻辑。
- HTTP/SSE transport 集中在现有 provider/API 层。
- 工具保留路径边界、输出上限和审批；subagent 保持单层。
- 不为“兼容旧代码”增加本轮未要求的分支或迁移。

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

业务代码除 mock 外还要做真实本地测试。HTTP/provider 测试不得调用付费模型 API；Playwright 使用真实 backend、Vite 和本地假模型服务。Windows 临时目录 ACL 失败时使用当前用户可写的唯一 `--basetemp`，不得删除其他任务目录。

## 安全

浏览器写请求只允许配置的 loopback Origin；无 Origin 的本地 CLI 请求允许，CORS 不启用凭据。项目 Skills 和外部 MCP 输出都视为不可信，必须经过信任、schema、workspace 边界和审批。
