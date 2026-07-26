# Mini-Agent

Mini-Agent 是一个面向学习与实验的 Python Agent Harness。它用可观察的终端界面展示模型决策、工具调用、规划、审批、上下文压缩、持久化恢复、项目级 Skills、MCP 工具服务和并发 Subagents。

当前版本以 Textual TUI 为主要界面，要求 Python 3.11+ 和 PostgreSQL 16。模型侧目前提供 DeepSeek Chat Completions 适配器；`--planner rule` 可离线验证基础工具路径，但应用仍需要 PostgreSQL 保存 session 与 checkpoint。

## 已实现能力

- **执行与规划**：支持 `reactive`、`dynamic_replan` 和自动策略选择；独立的只读 Plan mode 可调研、提问并提交 Plan Review。
- **安全工具**：提供 workspace-confined 的文件读取、搜索、写入和精确编辑，以及网页搜索、受 SSRF 防护的网页抓取和跨平台命令执行；写入、命令和联网操作需要审批。
- **项目级 Skills**：发现 `.mini_agent/skills/<name>/SKILL.md`，支持模型语义选择、`$name` 显式激活、运行快照和 Plan→Agent 交接。
- **并发 Subagents**：父 Agent 可把独立工作交给多个子 Agent 并发执行；批次结果持久化，同路径写入和命令执行有进程内协调。
- **MCP Server**：通过 stdio 暴露无需交互审批的 `read_file`、`glob`、`grep`，复用工具参数校验和 workspace 边界。
- **耐久运行**：PostgreSQL 保存 session、运行时快照、审计事件和 checkpoint；恢复时不会静默重放结果不确定的副作用。
- **可观察 TUI**：流式展示 reasoning、响应、工具与 Subagent 事件；支持详情级别、下一轮消息队列、历史和结构化 trace。
- **上下文与审计**：按上下文窗口估算 token，支持自动或手动压缩；JSONL 与 PostgreSQL 保存递归脱敏的运行轨迹。

尚未完成的主要方向包括强执行沙箱、统一的全局时间/费用/工具输出预算、后台进程管理、Replay/Eval、浏览器前端和跨进程 Subagent 调度。当前 Subagents 是单进程、单层并发能力，不是分布式多 Agent 系统。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

docker compose up -d
Copy-Item .env.example .env
```

Compose 会创建开发数据库 `mini_agent` 和测试数据库 `mini_agent_test`。示例凭据只适合本机开发；使用默认 LLM planner 前需要在 `.env` 填写真实模型配置。

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

将 `.env.example` 复制为 `.env`。进程环境变量优先于文件值。

| 变量 | 默认/要求 | 用途 |
| --- | --- | --- |
| `PROVIDER` | `deepseek` | Provider 适配器；当前仅支持 `deepseek`。 |
| `API_KEY` | LLM planner 必填 | 模型凭据，不会写入日志。 |
| `BASE_URL` | LLM planner 必填 | 服务根地址、`/v1` 地址或完整 `/chat/completions` 地址。 |
| `MODEL` | LLM planner 必填 | Provider 侧模型名称。 |
| `MAX_TOKENS` | `8192` | 单次输出上限，范围 1–384000。 |
| `CONTEXT_SIZE` | `1024000` | 上下文窗口，必须大于 `MAX_TOKENS`。 |
| `TOKENIZER_MODEL` | `deepseek-ai/DeepSeek-V3` | 本地 token 估算器。 |
| `DATABASE_URL` | 必填 | PostgreSQL session/checkpoint 存储。 |
| `LOG_FULL_MESSAGES` | `true` | 保存完整脱敏 body；设为 `false` 时保存摘要。 |

`.env`、日志、缓存和本地运行数据已被 Git 忽略。不要把真实密钥、认证头、完整环境变量或生产数据写入仓库和测试 fixture。

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
| `/new [title]`、`/clear [title]` | 准备新 session；不会删除旧 session。 |
| `/help`、`/quit` | 查看帮助或退出。 |

任务中可用 `@relative/path` 注入 workspace 文件，也可用 `$skill-name` 显式激活 Skill。Plan mode 只暴露只读调研工具以及 `request_user_input`、`request_plan_review`；只有后者会打开 Plan Review，批准后会创建独立、可审计的 Agent run。

## 项目级 Skills

Skill 位于 `<workspace>/.mini_agent/skills/<skill-name>/SKILL.md`：

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

仓库中的 `.mini_agent/skills/` 提供示例开发工作流。无效 Skill 会在启动时快速失败并指出文件和原因。

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

## 架构

```text
TUI -> ConversationService -> AgentRunner -> workflows -> planner / tools
                                  |              |
                                  |              +-> Skills / Subagents
                                  +-> RuntimeEvent / checkpoints

MCP stdio -> McpToolAdapter -> safe ToolRegistry subset
LLMClient -> JsonHttpTransport <-> DeepSeek adapter
PostgreSQL <-> sessions / RuntimeState / audit events / checkpoints
JSONL      <-> per-run redacted diagnostics
```

- `src/backend/domain/`：provider-neutral message、plan、session、skill 和 run state。
- `src/backend/planning/`：规则/LLM planner、上下文管理、模型请求生命周期和结构化输出。
- `src/backend/runtime/`：应用装配、conversation、workflow、Plan mode、hooks、恢复和 Subagent 协调。
- `src/backend/providers/`：通用 HTTP/SSE transport、Provider 门面和 DeepSeek wire adapter。
- `src/backend/tools/`：ToolRegistry、JSON Schema、文件、网页、命令和 delegation tool。
- `src/backend/mcp/`：只读 stdio MCP adapter。
- `src/backend/storage/postgres/`：PostgreSQL schema、session 和 checkpoint adapter。
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

测试会重置 `mini_agent_test` 的 `public` schema，绝不能把 `TEST_DATABASE_URL` 指向开发或生产数据库。CI 在 Python 3.11–3.13 和 Windows/Linux 上检查 Ruff，并在 PostgreSQL 服务上运行带 70% 门槛的 backend 覆盖率测试。开发约定详见 [docs/development.md](docs/development.md)。
