# Runtime Execution

该包把 Planner 输出执行为模型调用、Tool step 和终态。

- `__init__.py`：公开 `RuntimeRunner` 执行协议，不装配具体 runner。
- `runner.py`：执行循环门面；`steps.py`：单步模型/tool 调度。
- `contracts.py`：step/workflow 数据协议；`skills.py`：Skill 注入。
- `legacy.py`：当前仍使用的旧执行适配边界，不扩展新业务。
- `lifecycle/`：取消、interrupt、发布和终态。
- `workflows/`：执行/提案工作流、预算和控制。

执行层通过 Runtime Core/Tool 接口工作；终态必须统一由 lifecycle 完成。
