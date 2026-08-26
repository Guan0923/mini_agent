# Mini-Agent 仓库协作规范

## 项目定位

Mini-Agent 是一个 local-first Agent 应用：浏览器前端通过 loopback HTTP/SSE 访问本机 backend；backend 承载 Agent Runtime、模型调用、工具、会话和工作区；可选 cloud 服务负责账户、邮件验证、加密同步和 PostgreSQL。当前客户端方向是 Web，`tui/` 仅作为遗留兼容入口。

以当前源码、测试、`pyproject.toml`、`frontend/package.json` 和运行配置为准；README 或历史文档与源码冲突时，先核对实际实现再更新文档。

## 协作与变更边界

- 动手前先用简短中文说明要做什么、为什么做，以及会验证什么。
- 先检查 `git status --short` 和相关文件；保留用户已有修改、未跟踪文件和临时产物。
- 禁止未经明确授权执行 `git reset --hard`、`git checkout --`、递归删除、清理整个工作树或覆盖无关文件。
- 优先使用 `apply_patch` 做有意编辑；机械格式化可使用专用工具，但要检查差异。
- 只完成用户授权范围内的变更；不要顺手提交 commit、推送、启动长期服务或修改外部数据库。
- 读取日志、配置、环境变量和测试输出时脱敏；不要回显密钥、Cookie、认证头、sync token、SMTP 密码或完整进程环境。
- 计划模式中涉及取舍的决定由用户作出：先解释目的、影响和选项，再使用对应的用户输入工具，不要替用户拍板。

## 架构与依赖方向

```text
frontend/ ── HTTP/SSE ──> backend (127.0.0.1:8000)
                              ├─ runtime / planning / providers / tools
                              ├─ local SQLite / MCP / Sandbox
                              └─ optional HTTPS ──> cloud (8100) ──> PostgreSQL
```

- `backend/src/domain/`：与 provider、存储和展示无关的消息、计划、会话、Skill、错误和运行状态。
- `backend/src/planning/`：规则/LLM planner、上下文管理、模型请求生命周期和结构化输出。
- `backend/src/runtime/`：应用装配、会话编排、工作流、Plan mode、恢复、hooks、事件和单层 subagents。
- `backend/src/providers/`：通用 JSON/SSE transport 与 provider 适配；不得导入 storage 实现。
- `backend/src/tools/`：ToolRegistry、Schema、文件/网页/命令/delegation 工具；工具必须保留路径边界、输出上限和审批策略。
- `backend/src/mcp/`：安全只读 stdio server 与审批型外部 MCP 客户端。
- `backend/src/storage/`、`sync/`、`observability/`、`sandbox/`：本地存储、加密同步、脱敏日志和沙箱边界。
- `frontend/`：React/Vite/TypeScript 客户端，只调用 backend API，不导入 Python 或 backend 实现模块。
- `cloud/`：账户、邮件、设备授权、密钥封装和加密快照服务；只有 cloud 访问 PostgreSQL、SMTP 和 cloud 主密钥。
- `tests/`：Python 端到端/契约/单元测试；前端测试位于 `frontend/src/`。

依赖必须向内：domain 不依赖外层；runtime 依赖 planner/tool 端口；provider 不依赖 storage；backend 不直连 PostgreSQL；frontend 不绕过 API。严格 Sandbox Broker 未就绪时不得静默降级为普通进程。

### 遗留 TUI

不要为新功能扩展 `tui/src/`。只有在修复现有兼容性或已有测试时才修改它；Web 行为应实现于 frontend/backend API。遗留入口包括 `run.py`、`python -m tui` 和 `mini-agent` 命令。

## 常用命令

从仓库根目录运行：

```powershell
uv sync
cd frontend; npm ci; cd ..

# backend（默认 127.0.0.1:8000）
uv run python -m backend.api

# frontend 开发服务器（默认 127.0.0.1:5173，另开终端）
cd frontend; npm run dev
```

可选 cloud/PostgreSQL：

```powershell
$env:MINI_AGENT_SECRET_KEY = "replace-with-a-32-byte-development-secret"
docker compose up -d postgres cloud
$env:CLOUD_URL = "http://127.0.0.1:8100"
```

生产本地模式先执行 `cd frontend; npm run build`，再启动 backend；backend 会托管 `frontend/dist`。TUI 仅用于遗留验证，例如 `uv run python run.py --planner rule "calculate 1 + 1"`。

## 编码与实现约束

- Python 使用四空格、类型标注、`snake_case` 函数/变量、`PascalCase` 类；公共接口保持简洁并写模块 docstring。
- TypeScript/React 遵循现有组件和 API 层模式；不要在组件中复制请求、鉴权、错误分类或状态持久化逻辑。
- HTTP/SSE 传输集中在 `backend/src/providers/transport.py` 或现有 API 层；provider 特有转换放在对应 provider 包。
- 复用已有的规范化、校验、路径限制和持久化 helper；不要为满足文件行数人为拆出转发模块。
- 保持 subagent 单层；除非同时设计预算、取消、持久化和递归协调，否则不要递归 delegation。
- 运行时发布 `RuntimeEvent`，展示逻辑留在客户端；不要把 UI 行为塞进 runner 或工具实现。
- Windows Broker 安装必须沿用单次 UAC 事务、受控 helper 和安全错误分类；原始路径、命令行和异常只进脱敏日志。

## 测试与验证

修改后按风险运行最小但有代表性的验证：

```powershell
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m pytest -q

cd frontend
npm run typecheck
npm test -- --run
npm run build
```

- pytest 文件命名为 `test_*.py`，测试函数命名为 `test_<behavior>`；每个契约或状态迁移保留一个聚焦测试。
- 覆盖 provider 解析/重试、脱敏、持久化迁移、路径限制、审批、命令执行、Sandbox 安装失败分类和 API 错误体。
- HTTP/provider 测试使用 mock，绝不调用付费模型 API；外部 cloud/PostgreSQL 只在明确的集成测试中启用。
- Windows pytest 若默认临时目录 ACL 失败，使用当前用户可写的唯一 `--basetemp`；不要删除其他任务的临时目录。
- 若测试导入了另一个 worktree 的 editable 包，先检查 `module.__file__`，改用当前环境的 `uv run`/隔离环境后再判断代码是否失败。

## 数据与安全

- 浏览器会话缓存位于 `~/.mini_agent-cache/auth/client.db`；用户配置和运行数据位于 `~/.mini_agent/<user_id>/`，其中 `user.db` 保存加密 Provider/sync 状态，`runtime/<session_id>/` 保存会话 SQLite、workspace 和 uploads。
- 项目 `.mini_agent/skills/` 和外部 MCP 输出均视为不可信；必须经过 Skill 信任、Schema、workspace 边界和审批。
- 不要把 API Key、Cookie、认证头、同步 token、SMTP 凭据、cloud 主密钥、完整环境变量或真实用户数据写入代码、测试快照、日志或提交。
- 保持递归脱敏；`runtime.log_full_messages` 为 true/false 时，JSONL、SQLite、同步快照、运行状态和历史投影仍须保持兼容。

## 提交说明

除非用户明确要求，不创建 commit。需要提交时使用聚焦的 Conventional Commit 消息，例如 `fix: classify sandbox broker failures`，并在说明中列出行为变化、验证命令和已知限制。
