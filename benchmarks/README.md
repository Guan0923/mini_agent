# Mini-Agent Benchmark（精简版）

给 mini-agent 出的一套"考试题"：把任务交给 agent 去做，然后自动判卷、算分、出成绩单。
目前共 4 道题，覆盖 3 个能力（工具 / Skills / MCP），子代理题、AI 判卷、并行等留到后续版本。

## 怎么跑

在仓库根目录执行（用 `uv` 管理环境）：

```bash
# 查看有哪些题
uv run python -m benchmarks.run --list

# 免费离线冒烟：不调用模型，验证考试系统本身没写错（期望 score 1.0）
uv run python -m benchmarks.run --task tools-read-file --planner rule

# 真实模型跑全部题
uv run python -m benchmarks.run --all --output report.json

# 只跑某一题 / 只测某个能力
uv run python -m benchmarks.run --task skills-write-note
uv run python -m benchmarks.run --capability tools
```

运行前提：`~/mini_agent/config.toml` 里要配好 `[model]` 的 `api_key` / `base_url` / `model`
（任意 OpenAI 兼容接口都行，比如 vLLM、Ollama 或本地代理）。没配好时 llm 模式会报错提示你配置，
或者用 `--planner rule` 免费跑。

## 成绩单

输出是 JSON，默认写在 `benchmarks/output/<时间戳>/report.json`。结构：

- `summary` —— 总分、及格率、各能力得分、平均耗时/ token
- `tasks` —— 每道题的状态、分数、耗时、模型调用次数、token 用量、判卷明细

## 怎么加新题

1. 在 `benchmarks/tasks/` 下新建一个模块（参考已有的 `tools_filesystem.py`），
   定义 `TASKS = (BenchmarkTask(...),)`：

   - `prompt` —— 发给 agent 的任务文本（显式用技能时写 `$技能名`）
   - `seed` —— 提前铺好的初始文件 / 技能 / MCP 配置
   - `checkers` —— 判卷检查器：文件存不存在、内容对不对、某个工具是不是真的调了
   - `budgets` —— 每题的最多模型轮数 / 工具调用数（控制成本）
   - `planner_modes` —— 能离线跑（`"rule"`）还是只能真实模型（`"llm"`）

2. 在 `benchmarks/tasks/__init__.py` 里导入并加进 `ALL_TASKS`。

判断"改好了还是改差了"：改完代码后重跑，看分数和耗时/token 变化。注意 LLM 有随机性，
同一题多跑几次看中位数更可靠。

## 目录

```
benchmarks/
  run.py                 命令行入口
  model.py               任务 / 判卷的数据结构
  sandbox.py             隔离环境（不碰你真实的 ~/mini_agent）
  runner.py              把任务交给 agent 并收集指标
  event_collector.py     收集事件（耗时、调用次数、token）
  grading/               判卷（检查器 + 算分）
  report.py              成绩单
  tasks/                 题目定义
  mcp/mock_server.py     MCP 题用的迷你模拟服务器
  output/                运行产物（已 gitignore）
```
