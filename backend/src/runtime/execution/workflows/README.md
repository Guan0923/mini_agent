# Execution Workflows

该包实现 Agent 执行和 Plan 提案的业务流程。

- `__init__.py`：公开两类 workflow 及其共享预算/修复入口。
- `execution.py`：正常模型/tool 循环。
- `proposal.py`：Plan Review/提案路径。
- `budgets.py`：模型 Turn 与 Tool 调用预算。
- `controls.py`：pause/steering/interrupt 控制。
- `common.py`：两类 workflow 真正共享的步骤。

workflow 使用 Runtime Core 状态和 lifecycle 终态；预算拒绝与用户控制不得被当作普通 Provider 错误。
