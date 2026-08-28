# Runtime Persistence Support

该包封装执行过程中的 checkpoint 与 recording 协调。

- `checkpointing.py`：创建/加载恢复 checkpoint。
- `recording.py`：运行事件、消息和 provenance 的持久化调用。
- `__init__.py`：公开持久化辅助接口。

具体事务由 storage 实现；该包只协调 Runtime 时机，不直接执行 SQL。
