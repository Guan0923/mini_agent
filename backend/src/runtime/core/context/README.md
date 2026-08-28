# Runtime Context

该包拆分单次 Agent 运行的状态、服务和模型交换。

- `state.py`：`RuntimeState`、`RunSummary` 和执行中可变状态。
- `runtime.py`：`RuntimeServices`、`AgentRuntime` 门面及消息文本投影。
- `exchange.py`：`RuntimeExchange`、`PreparedResponse`、tool/exchange ID 和成功 Item 过滤。
- `__init__.py`：保持 `runtime.core.context` 入口。

Context 的可变状态仅属于一次运行；Provider 上下文只能包含成功 Item，并保留 tool call/result 关联。
