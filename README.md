# Mini-Agent

Mini-Agent 是一个面向学习与实验的 Python Agent Harness。它用可观察的终端界面展示模型决策、工具调用、规划、审批、上下文压缩、持久化恢复、项目级 Skills、MCP 工具服务和并发 Subagents。

当前版本以 Textual TUI 为主要界面，要求 Python 3.11+。客户端以每会话 SQLite 为唯一运行时存储；PostgreSQL 只用于可选的远端同步服务，客户端不会直连数据库。

## 已实现能力

- **执行与规划**：支持 `reactive`、`dynamic_replan` 和自动策略选择；独立的只读 Plan mode 可调研、提问并提交 Plan Review。
- **安全工具**：提供 workspace-confined 的文件读取、搜索、写入和精确编辑，以及网页搜索、受 SSRF 防护的网页抓取和跨平台命令执行；写入、命令和联网操作需要审批。
- **分层 Skills**：合并 `~/mini_agent/skills` 与项目 `.mini_agent/skills`，同名时项目版本完整覆盖全局版本。
- **并发 Subagents**：父 Agent 可把独立工作交给多个子 Agent 并发执行；批次结果持久化，同路径写入和命令执行有进程内协调。
- **MCP**：既可通过 stdio 暴露安全只读工具，也可从全局/项目 `mcp.toml` 加载外部 server；外部工具始终需要审批且不进入 Plan mode。
- **耐久运行**：`~/mini_agent/<session_id>/state.db` 保存完整本地状态和同步 outbox；离线或服务端失败不影响运行。
- **可观察 TUI**：流式展示 reasoning、响应、工具与 Subagent 事件；支持详情级别、下一轮消息队列、历史和结构化 trace。
- **上下文与审计**：按上下文窗口估算 token，支持自动或手动压缩；`~/mini_agent/logs` 与会话 SQLite 保存递归脱敏的运行轨迹。

尚未完成的主要方向包括强执行沙箱、统一的全局时间/费用/工具输出预算、后台进程管理、Replay/Eval、浏览器前端和跨进程 Subagent 调度。当前 Subagents 是单进程、单层并发能力，不是分布式多 Agent 系统。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python run.py --planner rule "calculate 1 + 1"
```

首次启动会创建 `~/mini_agent/config.toml` 和稳定的 device ID。若项目仍有旧 `.env`，启动器会原子迁移其受支持字段、校验 TOML 后删除旧文件；之后客户端只读 TOML，不接受进程环境覆盖。使用默认 LLM planner 前请填写 `[model]`。

```powershell
# 交互式 TUI；三个入口等价
python run.py
python -m tui
mini-agent

# 单任务、离线规则规划器、恢复会话
python run.py "整理并总结 README.md"
python run.py --planner rule "calculate (18 + 6) * 4"
mini-agent --resume session_xxx
```

主要 CLI 参数：

```text
--workspace PATH
--planner llm|rule
--strategy auto|reactive|dynamic_replan
--max-model-turns N
--max-tool-calls N
--max-retries N
--max-model-repairs N
--max-transport-retries N
--max-tool-recoveries N
--max-replans N
--log-dir PATH
--resume SESSION_ID
```

`--max-actions` 仅作为 `--max-tool-calls` 的废弃兼容别名，不能与后者同时使用。

## 配置

唯一客户端配置是 `~/mini_agent/config.toml`；仓库中的 `config.toml.example` 可作为模板：

```toml
[model]
provider = "deepseek"
api_key = "replace-with-your-key"
base_url = "https://api.deepseek.com"
model = "deepseek-chat"
max_tokens = 8192
context_size = 1024000
tokenizer_model = "deepseek-ai/DeepSeek-V3"

[runtime]
log_full_messages = true

[sync]
# device_id 由客户端自动生成
# url = "https://sync.example.com"
# token = "replace-with-a-sync-bearer-token"
```

同步只接受无 URL 凭据、query 或 fragment 的 HTTPS endpoint；网络失败会保留本地 outbox，并在下次启动、checkpoint 或正常退出时重试，不进行固定轮询。不要提交真实模型密钥、同步 token、认证头或生产数据。

## TUI 工作流

TUI 使用 alternate screen，消息区可滚动，状态栏和输入框固定在底部。Enter 提交，Ctrl+J 换行；运行中提交的普通消息会排队，在当前 run 完成或 Esc 协作式取消后合并为下一轮。审批出现时，审批选项独占输入区域。

| 命令 | 说明 |
| --- | --- |
| `/agent`、`/plan` | 切换正常执行和只读规划/讨论模式。 |
| `/permission` | 选择当前进程的逐次审批或 Full access。 |
| `/display [minimal\|medium\|verbose]` | 选择 transcript 详情级别。 |
| `/skills`、`/tools` | 查看发现的 Skills 和当前工具。 |
| `/compact` | 立即压缩旧上下文。 |
| `/trace`、`/history` | 查看最近运行 trace 或当前会话历史。 |
| `/sessions`、`/resume [id]` | 列出并恢复持久化会话。 |
| `/fork [run_id]` | 从任意非运行中 run 创建当前设备拥有的新 session。 |
| `/time` | 选择会话时区，供模型通过时间工具按需查询。 |
| `/new [title]`、`/clear [title]` | 准备新 session；不会删除旧 session。 |
| `/help`、`/quit` | 查看帮助或退出。 |

任务中可用 `@relative/path` 注入 workspace 文件，也可用 `$skill-name` 显式激活 Skill。Plan mode 只暴露只读调研工具以及 `request_user_input`、`request_plan_review`；只有后者会打开 Plan Review，批准后会创建独立、可审计的 Agent run。

## 全局与项目 Skills

全局 Skill 位于 `~/mini_agent/skills/<skill-name>/SKILL.md`，项目 Skill 位于 `<workspace>/.mini_agent/skills/<skill-name>/SKILL.md`；同名时项目版本完整覆盖全局版本。manifest 示例：

```markdown
---
name: review-python
description: Review Python changes for correctness, typing, and tests.
---

# Workflow

Read changed files, inspect focused tests, and report concrete findings.
```

- 名称仅允许小写字母、数字和连字符，最长 64 字符，且必须与目录名一致。
- frontmatter 只允许 `name` 和 `description`；manifest 最大 64 KiB，正文最多 1000 行。
- LLM planner 先根据名称和描述选择 Skill，再把完整正文加入当前 run；Rule planner 不做语义选择。
- 激活内容、根目录和 SHA-256 会进入 checkpoint；恢复和 Plan handoff 使用原快照。
- Skill 可带 `scripts/`、`references/`、`assets/`，但不会注册新工具，也不能绕过 workspace、JSON Schema 或审批策略。

无效 Skill 会在启动时快速失败并指出文件和原因。

## 并发 Subagents

LLM 可调用 `delegate_tasks` 提交一组具有唯一 ID 的独立任务。每个子 Agent 拥有标准 workspace 工具、独立 run/session 状态和父 run 的取消信号；父 Agent 收到有序、可分页的结果后继续综合。

同一批任务在线程池中并发运行；同路径文件写入串行化，命令执行与所有文件写入互斥，审批仍由父交互通道处理。子 Agent 没有 delegation 工具，因此不会递归派生。恢复时，未完成批次标为 `indeterminate`。有先后依赖、共享状态或高冲突写入的任务应由父 Agent 顺序执行。

## MCP Server

```powershell
mini-agent-mcp --workspace C:\path\to\workspace
```

通用 MCP 客户端配置：

```json
{
  "mcpServers": {
    "mini-agent": {
      "command": "mini-agent-mcp",
      "args": ["--workspace", "C:\\path\\to\\workspace"]
    }
  }
}
```

MCP adapter 只暴露 `read_file`、`glob`、`grep`。写入、命令和联网工具不会通过该 server 暴露；所有参数仍经过共享 JSON Schema 校验，路径仍限制在指定 workspace 内。MCP server 本身不需要模型密钥或 PostgreSQL。

客户端也会合并 `~/mini_agent/mcp.toml` 与 `<workspace>/.mini_agent/mcp.toml`，同名 server 由项目配置完整覆盖：

```toml
[servers.example]
command = "example-mcp-server"
args = ["--stdio"]
# cwd = "C:/path/to/workspace"
# env = { EXAMPLE_TOKEN = "secret" }
```

发现的工具注册为 `mcp_<server>_<tool>`。外部 server 使用长生命周期 stdio session；其工具始终需要审批且不向 Plan mode 暴露。

## 架构

```text
TUI -> ConversationService -> AgentRunner -> workflows -> planner / tools
                                  |              |
                                  |              +-> Skills / Subagents
                                  +-> RuntimeEvent / checkpoints

MCP stdio -> McpToolAdapter -> safe ToolRegistry subset
LLMClient -> JsonHttpTransport <-> DeepSeek adapter
SQLite     <-> local sessions / RuntimeState / audit / checkpoints / outbox
HTTPS sync <-> optional PostgreSQL remote snapshots and idempotent operations
JSONL      <-> ~/mini_agent/logs redacted diagnostics
```

- `src/backend/domain/`：provider-neutral message、plan、session、skill 和 run state。
- `src/backend/planning/`：规则/LLM planner、上下文管理、模型请求生命周期和结构化输出。
- `src/backend/runtime/`：应用装配、conversation、workflow、Plan mode、hooks、恢复和 Subagent 协调。
- `src/backend/providers/`：通用 HTTP/SSE transport、Provider 门面和 DeepSeek wire adapter。
- `src/backend/tools/`：ToolRegistry、JSON Schema、文件、网页、命令和 delegation tool。
- `src/backend/mcp/`：只读 MCP adapter 与分层外部 stdio MCP client。
- `src/backend/storage/sqlite.py`、`src/backend/sync/`：本地会话存储、同步客户端和远端 PostgreSQL 服务。
- `src/backend/observability/`：事件扇出、JSONL 记录与脱敏。
- `src/tui/`：CLI、Textual 组件、screen、rendering、view 和 widget。
- `src/frontend/`：未来浏览器前端占位；只能通过版本化 backend API 通信。

依赖保持向内：domain 不依赖外层；provider 不导入 TUI 或 storage；TUI 只组合 runtime 服务并渲染 `RuntimeEvent`。详细契约见 [docs/architecture.md](docs/architecture.md)。

## 开发与验证

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
python -m pytest --cov=backend --cov-report=term-missing
```

客户端与大多数测试不需要 PostgreSQL；服务端/旧适配器集成测试会重置 `mini_agent_test` 的 `public` schema，绝不能把 `TEST_DATABASE_URL` 指向开发或生产数据库。开发约定详见 [docs/development.md](docs/development.md)。
