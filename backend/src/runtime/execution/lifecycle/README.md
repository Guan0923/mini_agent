# Execution Lifecycle

该包集中处理一次运行的开始、取消、interrupt、事件发布和终态。

- `cancellation.py`：provider/tool 取消绑定。
- `interrupts.py`：审批/用户输入 interrupt。
- `outcomes.py`：`complete_run`、`fail_run`、`cancel_run`、`pause_run`。
- `publisher.py`：`RunEventPublisher`；`finalization.py`：资源和状态收口。
- `__init__.py`：生命周期公开入口。

任何成功/失败/暂停都必须经过这里，避免多处分散写终态或遗漏资源释放。
