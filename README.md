# MiniHermes Lab 项目计划书

## 一、项目背景

现有 Hermes、OpenClaw、LangGraph 等框架已经能快速搭建 Agent，但大量核心机制被封装，使用者很难真正理解 Agent 如何规划任务、选择工具、管理上下文、处理失败和恢复执行。

本项目不复刻完整 Hermes，也不追求商业化，而是开发一个轻量级 Agent 实验平台，用于研究和验证 Agent 的核心运行机制。

项目目标是实现一个能够：

- 完成多步骤任务；
- 调用外部工具；
- 保存任务状态；
- 在失败后恢复；
- 管理长上下文；
- 对不同 Agent 策略进行评测和对比；

的通用 Agent。

项目最终需要证明的不是“功能很多”，而是：

**你能够理解、实现并评测一个 Agent 从接收任务到完成任务的完整过程。**

------

## 二、核心功能

| 模块       | 功能                                                     |
| ---------- | -------------------------------------------------------- |
| 任务执行   | 接收复杂任务，持续执行多个步骤，直到完成、失败或达到限制 |
| 工具调用   | 支持文件、网页、搜索、计算、数据分析和命令执行等工具     |
| 任务规划   | 支持逐步决策、先规划后执行、失败后动态重规划             |
| 错误处理   | 处理参数错误、工具失败、超时、重复调用和执行偏离         |
| 上下文管理 | 支持完整历史、滑动窗口、摘要压缩和关键事实保留           |
| 任务记忆   | 保存目标、约束、已完成步骤、未完成步骤和重要结果         |
| 状态恢复   | 程序中断后从最近进度继续执行，避免重复完成步骤           |
| 权限控制   | 文件修改、删除和危险命令等操作必须经过用户确认           |
| 运行轨迹   | 展示每一步决策、工具调用、错误、重试和最终结果           |
| 回放对比   | 使用不同模型、规划和记忆策略重新运行同一个任务           |
| 自动评测   | 统计成功率、步骤数、工具错误、恢复率、耗时和成本         |

首版重点验证三类问题：

1. 不同规划策略对任务成功率有什么影响；
2. 不同上下文策略如何影响成本和信息遗失；
3. Agent在工具失败和程序中断后能否正确恢复。

------

## 三、实施规划

项目周期建议为六周。

| 周次  | 目标               | 主要成果                                     |
| ----- | ------------------ | -------------------------------------------- |
| 第1周 | 完成最小执行闭环   | Agent能够接收任务、调用工具并输出结果        |
| 第2周 | 完善工具和错误处理 | 参数校验、超时、重试、重复调用检测和权限确认 |
| 第3周 | 实现规划策略       | 支持逐步决策、预先规划和动态重规划           |
| 第4周 | 实现记忆和恢复     | 上下文压缩、任务记忆、暂停和断点恢复         |
| 第5周 | 建立轨迹和评测     | 运行记录、任务回放、策略对比和自动评分       |
| 第6周 | 完成实验和展示     | 评测报告、失败案例、演示任务和项目文档       |

### 第一阶段：基础执行

完成任务输入、工具调用、执行次数限制和基础运行记录。

验收标准：

- 能连续调用多个工具；
- 能根据工具结果继续执行；
- 达到限制后能够正常停止；
- 至少完成5个基础任务。

### 第二阶段：可靠性

增加工具参数检查、失败重试、超时、重复调用检测和危险操作确认。

验收标准：

- 工具错误不会导致系统崩溃；
- Agent能够修正部分错误；
- 不会无限重复同一操作；
- 高风险行为必须得到用户批准。

### 第三阶段：规划能力

实现三种规划策略，并使用相同任务进行比较。

验收标准：

- 同一任务可以切换不同策略；
- Agent能够记录计划完成状态；
- 工具失败后可以调整剩余计划；
- 至少完成10组策略对比。

### 第四阶段：上下文与恢复

实现上下文压缩、结构化记忆、任务暂停和断点恢复。

验收标准：

- 长任务中不会遗忘核心目标；
- 压缩后仍能保留关键约束；
- 程序重启后可以继续任务；
- 已完成步骤不会被重复执行。

### 第五阶段：评测系统

建立30—50个固定测试任务，覆盖文件处理、搜索、数据分析、长上下文、工具失败和危险操作。

重点指标：

- 任务完成率；
- 约束满足率；
- 工具调用正确率；
- 错误恢复率；
- 任务恢复率；
- 平均步骤数；
- 平均耗时；
- 模型调用成本；
- 未经批准的危险操作次数。

### 第六阶段：求职展示

最终交付内容包括：

- 可运行的 Agent 平台；
- 至少6种工具；
- 3种规划策略；
- 3种上下文或记忆策略；
- 断点恢复能力；
- 完整运行轨迹；
- 30—50个评测任务；
- 策略对比实验报告；
- 典型失败案例；
- 3个稳定演示任务；
- 项目说明和技术复盘。

首版不做多 Agent 协作、视觉浏览器操作、自动生成 Skill、LoRA 微调、多用户系统和复杂前端。

项目完成标准：

**能够稳定运行多步骤任务，能够解释每一次决策，能够在失败后恢复，并能够用真实实验数据比较不同 Agent 策略。**

------

## 七、当前实现：终端演示版

首版使用 TUI（终端交互界面）而非 Web/GUI，以便直接观察 Agent 的计划、工具调用、重试和结果。运行环境为 Python 3.11+。

```bash
# 交互模式
python run.py

# 安装为本地命令（推荐）
python -m pip install -e .
mini-agent

# 单次演示
python run.py "calculate (18 + 6) * 4"
python run.py "读取 README.md"
python run.py "list files"
```

TUI 有两种模式，默认是 Agent 模式：模型每次选择工具调用或直接回复，而不会预先生成完整计划。系统以 provider-neutral Message 保存完整上下文；顶层会话只包含 System/User/Assistant，工具调用和结果作为 ToolMessage 嵌套在 AssistantMessage 中。对 DeepSeek，工具决策使用原生 Tool Calls，SSE `reasoning_content` 会实时显示为 `THINKING`，最终文本显示为 `RESPONSE`。

正常产品路径只会自动选择 `reactive` 或 `dynamic_replan`：前者适合简单、逐步确定的任务，后者会按实时工具结果生成并替换后续可执行阶段。`plan_execute` 仍可通过 `--strategy plan_execute` 显式启用，但它是“固定计划、失败即停止”的实验对照基线，自动路由和 `/plan` 都不会使用它。

输入 `/plan` 会进入只读规划与讨论模式：普通问候、解释和需求讨论直接作为对话返回；本地计算、列目录和读取文件可用于调研，网页搜索/抓取仍会要求确认，写入、移动和删除会被阻止。若项目本身无法回答会实质影响方案的问题，模型可单独调用 Plan 专用控制工具 `request_user_input`。只有当完整实施计划确实需要用户审核时，模型才单独调用 `request_plan_review` 并进入 `PLAN REVIEW`。两个控制工具都不注册到 `ToolRegistry`，也不计入 `/tools` 展示的执行工具。

Plan 调研、控制工具调用和结构化结果都作为普通 typed messages 保存在 session runtime history。`request_plan_review` 的计划正文保存在该调用的参数中，同一历史不会再插入重复计划消息；隔离 handoff 仍通过 run 的 `final_answer` 为新 session 提供一条计划 AssistantMessage。`PLAN REVIEW` 保留三个选择：`Implement`、`Implement and Clear Session` 和 `Cancel and Stay in plan mode`。handoff 后仍由正常策略路由器选择执行方式，不强制 `dynamic_replan`。

工具仅在其声明需要确认时才请求 Human-in-the-Loop：网页搜索/抓取、写文件、移动、删除和命令执行均会逐次确认；本地计算、列目录和读文件自动执行。TUI 默认使用 `Approval for me`；输入 `/permission` 可在当前程序内切换为 `Full access`，使所有工具审批自动 Continue。工具审批仍使用 `Continue / Cancel / Supplement`，Supplement 只属于 Tool Review，与 Plan Review 相互独立；`Full access` 不会跳过 `/plan` 生成提案后的 `PLAN REVIEW`，最终计划仍需人工选择 `Implement / Implement and Clear Session / Cancel and Stay in plan mode`。安全只读工具可按 `--max-retries` 同参数重试。若 reactive 或 `/plan` 调研中的工具最终失败，运行时会将截断后的调用和错误作为不可信上下文交给 LLM，请其最多连续纠错 `--max-tool-recoveries` 次（默认 2）；任一工具成功会重置该计数。不可自动重试的写入、移动、删除和命令调用不会因相同参数被重复执行。

交互式 TUI 使用 alternate screen：可滚动消息区占满终端，状态栏和输入框固定在最底部；状态栏始终包含当前 permission 模式。输入框按显式换行和软换行从 1 行自动增高到最多 4 行，超过后保持 4 行并在内部滚动；Enter 提交完整消息，Ctrl+J 在光标处插入换行。退出后会恢复进入前的终端画面，并输出当前 session ID、`/use <session_id>` 与 `mini-agent --session-id <session_id>` 恢复方式。Agent 运行中提交的普通消息进入进程内的下一轮队列；当前 run 结束后，这些消息按提交顺序合并并自动启动一个 follow-up run。Esc 会协作式取消当前 active run（包括 Tool/Plan Review 和 Plan 问题），取消完成后发送队列；`/quit` 或 Ctrl+C 则取消、丢弃队列并退出。Plan 问题逐题显示在独立候选列表中：方向键循环选择，普通候选按 Enter 确认；选中“以上都不对”后按 Tab 进入自由输入，Enter 提交非空答案。非 Textual 运行使用数字选择并在最后一项后读取自由文本。

`/new <title>` 与 `/clear <title>` 清空当前 transcript 并进入待创建 session，不会修改 alternate screen 之外的终端内容。其它 TUI 命令：`/permission` 在底部输入栏切换当前程序的工具审批模式，`/help` 查看帮助，`/tools` 查看工具，`/trace` 查看上一次运行的结构化轨迹，`/sessions` 列出保存的对话，`/session` 查看当前对话信息，`/history` 查看当前 session 的历史消息，`/use <session_id>` 切换保存的对话，`/quit` 退出。待创建 session 只保存在内存中，终端显示 `SESSION PENDING — <title> (not saved yet)`；用户发送第一条消息时才生成 session ID 并写入 SQLite，未发送消息便退出不会留下空 session。`/clear` 不删除旧 session，可通过 `/sessions` 和 `/use <session_id>` 恢复；待创建状态会清除当前模型上下文、当前会话记录和上一次运行状态，但不改变进程内的 mode、permission 或工具配置。标题和 session ID 只接受空格参数，不支持 `/new/<title>` 或 `/use/<session_id>`。

交互模式支持带注释的命令实时补全：输入 `/p` 会显示 `/plan — Create a plan and open Plan Review.` 和 `/permission — Choose the in-memory tool approval mode.`，按 Tab 接受候选，方向键选择候选，Enter 提交；接受候选时只插入命令本身。已识别命令会按文本顺序先执行，剩余普通文字按原顺序合并为一次 task；例如 `你好 /plan` 和 `/plan 你好` 都会先启用 Plan mode，再运行一次“你好”。`/new`、`/clear` 和 `/use` 会消费其后的文字直到下一个命令作为参数，其他命令后的文字仍属于 task；`/quit` 会停止处理当前行且不会提交 task。命令可以出现在任务句子中，但前面必须有空格；文件路径和 URL 中的 `/` 不会被识别为命令。任务中可以使用 `@相对路径` 引用工作区文件，例如 `请总结 @README.md`；文件内容会在本次任务中以内嵌引用形式提供给 Agent，引用路径必须位于 workspace 内。每次运行会在 `<workspace>/logs/<run_id>.jsonl` 记录有序 runtime messages，包括模型请求/规范化响应、计划、审批、工具调用和结果；SQLite 同时保存按 session/run 查询的同一审计轨迹、`session_runtime` 快照、checkpoint 和供 `/history` 使用的 user/assistant 文本投影。`LOG_FULL_MESSAGES=True`（默认）记录完整消息正文；设为 `False` 时记录长度、哈希和最多 200 字符预览。两种模式都会脱敏 API key、Authorization、Cookie、token、password 与 secret 字段，且不会记录 HTTP headers 或原始 provider payload。每个 turn 结束时，Runtime 的 usage 会被该 turn 最后一次模型响应 usage 覆盖。使用 `--session-id <session_id>` 可在新的 CLI 进程中继续已有对话。

Agent 运行时输入框保持可用。期间提交的多条普通消息会按顺序进入进程内队列，并在当前 run 完成后合并启动一次新的 follow-up run；它们不会改变正在执行的 run。已经开始的外部操作不会被强制中止，Esc 会先进入 `CANCELLING`，等待协作式取消完成后再发送队列。运行期间斜杠命令不可用，工具或计划审批出现时输入框会优先切换为审批选项。

模型 reasoning 等高频输出会先进入线程安全队列，再以约 30 FPS 合并刷新，因此不会改写输入 Buffer、光标或补全菜单。屏幕 transcript 保留最近 200,000 个字符，完整轨迹仍由 SQLite 与 JSONL 保存。默认自动跟随最新输出；PageUp 或鼠标滚轮可暂停跟随，PageDown 或 Ctrl+End 回到末尾后恢复自动跟随。

当前已具备：

- 可观察的「直接决策 → 工具调用 → 工具结果 → 再决策/最终回复」执行闭环；
- 安全计算、文件列出、文件读取，以及需要用户确认的网页搜索/抓取、文件写入、移动、删除和跨平台命令执行工具；
- 安全同参数重试、LLM 工具失败恢复与结构化运行轨迹；
- 可替换的规划器接口，为下一步接入 LLM 规划策略保留边界。

### 当前需要完成：Harness 能力补全

当前实现已经具备实验型 Agent Runtime 的核心闭环，下一步需要补齐生产级 Harness 的环境控制、扩展机制和可靠性保证。以下项目均为当前未完成 backlog，按优先级推进。

#### P0：可靠性底座

- [ ] **上下文管理**：增加模型 token 预算、工具结果裁剪、滑动窗口、摘要压缩和关键事实保留，避免长 session 与大工具输出拖垮后续请求。
- [x] **Hooks**：为 run、模型请求和工具调用提供可注入、可取消、可审计的 before/after 生命周期接口。
- [ ] **工具输出治理**：统一限制字符数、行数、文件数和估算 token；超限结果应截断或转存 artifact，而不是直接进入模型上下文。
- [ ] **工具选择策略**：按任务、模式和风险动态缩小可用工具集合，并要求工具与用户目标直接相关，避免无关的 workspace 扫描或网络请求。
- [ ] **暂停与恢复**：基于现有 checkpoint 实现 durable `/resume`、`/cancel` 和 `/terminate`，恢复时不得重复执行已经产生副作用的操作。
- [ ] **执行隔离**：为命令和高风险工具增加 sandbox、文件系统/网络策略、资源限制及子进程树清理。
- [ ] **全局预算与取消**：限制 run 总时长、token、费用和工具输出预算，并能取消正在进行的模型请求、命令或后台进程。
- [ ] **模型协议恢复**：为空 JSON、纯空格响应和结构错误增加有限格式纠正重试、非流式降级及可选 model/provider fallback，同时保持副作用幂等。

#### P1：完整 Harness 工作流

- [ ] **结构化代码工具**：提供 search、glob、范围读取、patch 和 git diff 等原生工具，减少模型通过通用 shell 完成代码操作。
- [ ] **变更事务与审查**：支持 diff、批量审批、原子应用、失败回滚和 undo，使一次任务的文件修改形成可审查变更集。
- [ ] **后台进程管理**：支持启动、轮询输出、查看状态和停止 dev server、watcher 等长期进程。
- [ ] **Replay 与 Eval**：建立固定任务集、运行回放、自动评分、模型/策略对比和成本报告，将现有日志转化为可重复实验。
- [ ] **能力扩展协议**：支持动态工具发现、MCP/插件、能力协商和 schema 版本管理，避免工具只能静态编入默认 catalog。
- [ ] **并发与一致性**：增加跨进程锁、任务队列、session 隔离和崩溃后的 ownership recovery。

P0 完成后再系统推进 P1，避免先扩展功能数量而缺少可靠性边界。多 Agent、视觉浏览器、LoRA、多用户系统继续保持首版非目标。

### 模块结构

```text
tui/          终端交互，仅负责输入、输出与确认
runtime/      应用服务、依赖装配、策略路由、工作流、工具执行、checkpoint 边界与运行状态收尾
observability/事件扇出与 JSONL 持久化日志 Sink
planning/     规则/LLM 规划策略与显式能力协议
tools/        工具契约、通用注册表、默认 workspace 工具 catalog、计算器、受限文件操作与跨平台命令执行
providers/    LLM 门面、.env 配置和各厂商完整 API 适配器
storage/      SQLite checkpoint/session 与未装配的 artifact 持久化适配器
domain/       Message、ToolMessage、ToolSpec、RunState 与运行轨迹等纯数据模型
```

依赖只向内：TUI 只处理终端输入、确认和 `RuntimeEvent` 渲染；`ConversationService` 管理单轮执行与当前 session，`runtime.factory` 负责装配具体实现；`ToolRegistry` 只负责注册与调用策略，默认工具由独立 catalog 提供；SQLite 位于 `storage/`，不被执行工作流直接依赖。工具和模型提供方可以各自替换或单独测试。

### 大模型配置（无 SDK）

将 `.env.example` 复制为 `.env`，填写 `PROVIDER`、`API_KEY`、`BASE_URL` 和 `MODEL`。`PROVIDER` 目前支持 `deepseek` 并默认取该值。可选的 `MAX_TOKENS` 控制每次模型输出上限，默认 `8192`，允许范围为 `1` 到 `384000`；复杂 JSON 计划应提高该值以避免输出被中途截断。`.env` 已被 Git 忽略，密钥不会被提交。

默认启动方式会通过 Python 的 `requests` 直接向 DeepSeek 的 `BASE_URL/v1/chat/completions` 发送 HTTP 请求；项目没有使用模型 SDK。`providers/client.py` 中的 `LLMClient` 负责 Provider 选择、通用 JSON/SSE 传输和请求诊断，`providers/deepseek.py` 中的 `DeepSeek` 只负责请求与响应协议转换。接入其他 API 时增加独立 provider adapter，不改变 Agent 内部 Message、Runtime 或通用传输实现。

模型工具调用由 provider adapter 转成 AssistantMessage 内的 ToolMessage；Runtime 校验工具名和参数后按顺序执行，并在全部结果就绪后再次请求模型。策略选择、计划生成和重规划仍使用 JSON Output。`write_file`、`move_file`、`move_folder`、`delete_file`、`delete_folder` 和 `run_command` 都需经过终端的 Human-in-the-Loop 明确批准；直接调用 `ToolRegistry` 时仍需显式传入 `confirmed=True`。

DeepSeek tool arguments 和规划阶段的 JSON Output 都会在本地校验；无效 JSON、未知工具、缺失 call ID 或截断响应不会进入工具执行层。工具运行失败后的模型恢复预算与本地工具重试预算相互独立。

所有工具参数在调用 handler 前还会通过注册时缓存的 JSON Schema 校验，未知字段、缺失字段和错误类型会统一转成 `ToolError`。命令工具会从子进程环境中移除 API Key、Token、Secret 和 Password 类变量，避免已批准的命令意外继承模型凭据。

`web_search` 通过项目依赖的 `ddgr` 以 DuckDuckGo HTML 搜索执行，并只接收它的非交互 JSON 结果。`web_fetch` 由项目自行实现：只允许 HTTP/HTTPS 和 80/443 端口，解析后拒绝本机、私网、链路本地及保留地址；每次重定向都会重新验证目标，最多三跳；响应限制为 2 MB，只接受静态 HTML、纯文本和 JSON，并最多向模型返回 100,000 个字符。两种网页工具都是只读，但因为会向外部发送查询或请求网页，均需 Human-in-the-Loop 批准。网页搜索结果和抓取正文始终是**不可信外部内容**，模型不会把其中的指令视为可执行命令。动态渲染、登录页面、PDF 与图片 OCR 不属于当前版本范围。

```bash
# 使用 .env 中的大模型（默认）
python run.py "计算 (18 + 6) * 4"

# 不发起网络请求的离线演示
python run.py --planner rule "calculate (18 + 6) * 4"

# 搜索与抓取网页；两者都会请求确认
python run.py --planner rule "search Python documentation"
python run.py --planner rule "fetch https://docs.python.org/3/"

# 所有文件变更工具都会请求确认
python run.py --planner rule "move file draft.txt to archive/draft.txt"
python run.py --planner rule "delete folder recursive build"

# 调整运行限制
python run.py --max-actions 12 --max-retries 2 --max-tool-recoveries 2 "计算 (18 + 6) * 4"

# 强制固定计划、顺序执行策略（仅用于策略对比或调试；自动路由不会选择它）
python run.py --strategy plan_execute "读取 README.md"

# 强制动态重规划，并限制最多两次重规划
python run.py --strategy dynamic_replan --max-replans 2 "整理并更新项目说明"

# 在已有 session 中继续多轮对话
python run.py --planner rule --session-id session_xxx "继续刚才的任务"

# 将运行日志写入自定义目录
python run.py --log-dir .agent-logs "计算 2 + 2"
```

开发环境、质量检查和 CI 约定见 [`docs/development.md`](docs/development.md)。本地提交前至少运行 `ruff check .`、`ruff format --check .` 和 `python -m pytest -q`。
