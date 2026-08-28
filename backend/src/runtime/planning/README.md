# Runtime Plan Mode

该包连接 Planning 决策与 Runtime handoff。

- `mode.py`：Plan/Agent 模式选择与工具能力。
- `review.py`：Plan Review、DecisionRequest 和 `RunHandoff` 处理。
- `__init__.py`：公开 Plan mode workflow。

未经用户批准不得执行提案；handoff 必须保留已审阅计划和明确的执行语义。
