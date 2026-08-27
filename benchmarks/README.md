# Mini-Agent 开源基准适配套件

该套件包含 9 个自包含任务，分别改编自 Terminal-Bench、SWE-bench Lite 和 τ³-bench，用于测量 Mini-Agent 是否能生成正确产物、修复真实回归，或完成受策略约束的工具工作流。

这不是任何上游排行榜的官方实现，分数不能与上游官方成绩直接比较。

## 任务构成

| 来源 | 数量 | 能力类型 |
| --- | ---: | --- |
| Terminal-Bench | 3 | Terminal 产物任务 |
| SWE-bench Lite | 3 | 软件工程回归修复 |
| τ³-bench | 3 | MCP 工具工作流 |

每个任务都有独立 fixture、预算和确定性的 subprocess verifier。正式注册的 9 个任务均使用 `llm` planner；CLI 保留 `rule` 选项供 harness 使用，但当前没有正式 `rule` 任务。

## 列出与运行

以下命令均从仓库根目录运行。列出任务不会调用模型：

```powershell
conda activate dev
uv run python -m benchmarks.run --list
```

运行一个任务、多个任务或完整套件：

```powershell
uv run python -m benchmarks.run --task swe-requests-2317 --config C:\path\to\benchmark.toml
uv run python -m benchmarks.run --task task-a --task task-b --config C:\path\to\benchmark.toml
uv run python -m benchmarks.run --all --config C:\path\to\benchmark.toml --output report.json
```

`--task` 可重复，也接受逗号分隔的任务名。未提供 `--task` 时默认选择全部任务；还可通过 `--capability terminal|software_engineering|tool_workflow` 过滤。

## 模型配置

`llm` 是默认 planner，需要包含以下字段的 OpenAI-compatible TOML：

```toml
[model]
api_key = "..."
base_url = "http://127.0.0.1:8001/v1"
model = "model-name"
```

建议显式传入 `--config PATH`。若省略，当前 CLI 会读取 `~/mini_agent/config.toml`。配置只用于向每次运行的新 sandbox 播种模型设置；报告和日志不得输出 API Key。

## 重复、输出与调试

重复每个任务以观察模型波动：

```powershell
uv run python -m benchmarks.run --all --repeat 3 --config C:\path\to\benchmark.toml
```

`--repeat N` 要求 `N >= 1`。每次 attempt 都使用新的 workspace 和 MCP 状态；重复运行会按比例增加模型调用量和费用。报告保留每次 attempt，并按任务通过率等权计算总分。

常用选项：

| 选项 | 用途 |
| --- | --- |
| `--output PATH` | 指定 JSON 报告；默认写入 `benchmarks/output/<timestamp>/report.json` |
| `--sandbox PATH` | 指定 benchmark 客户端状态根目录 |
| `--keep-workspaces` | 保留每个任务的 workspace 以便调试 |
| `--max-tool-calls N` | 覆盖所有所选任务的工具调用预算 |

runner 不下载源码仓库、不安装任务依赖、不使用 Docker，也不在评分期间访问外部 Web 服务。除非使用本地模型端点，否则 `llm` planner 会访问配置的模型 API。

## 评分

任务只有在完整 verifier 通过时得 `1.0`，否则得 `0.0`。Verifier 检查语义和最终状态，不要求特定工具名或回答措辞。测试套件还验证一个关键不变量：未修改的 fixture 必须失败，测试专用 oracle 结果必须通过。

JSON 报告在 `runs` 中保存逐次结果，在 `tasks` 中保存按任务聚合的通过率。来源元数据包括上游 benchmark、task ID、固定 revision、URL、license 和本地适配说明。

## 来源与许可

详见 [`THIRD_PARTY_BENCHMARKS.md`](THIRD_PARTY_BENCHMARKS.md)。仓库只包含复现问题或工作流所需的最小代码/数据 fixture，不向 Agent workspace 提供 gold patch。
