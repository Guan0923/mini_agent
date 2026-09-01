# Conversation Runtime

该包实现 Session/Turn 的应用服务和用户输入生命周期。

- `__init__.py`：Conversation 子包标记；公开能力由 `service.py` 与 `ports.py` 定义。
- `service.py`：`ConversationService` 门面，启动 Turn，并让普通运行与恢复运行共用 Plan handoff；可在同一 Thread 中连续执行 Plan → Compact → Agent。
- `ports.py`：存储/事件依赖协议；`bridge_support.py`：Node bridge、compaction 与 resume bridge 所有权。Web SSE 附加 external bridge 后，恢复流程必须复用它；embedding 调用才创建 internal bridge。
- `session_control.py`、`user_input.py`、`steering.py`：Session 控制、消息输入和 FIFO steering。
- `recovery/`：恢复预览、attempt 重建与 resume；恢复只绑定一个 bridge，异常终态也只由该 bridge 的所有者写入。

对外通过 `ConversationService`/ports；不直接依赖 FastAPI，Turn 树写入交由 node bridge/storage。
同一 Turn 不允许两个动态 bridge 并行投影，因为 `decision_requested` 等交互 Item 的 Trace 坐标是不可变的。
