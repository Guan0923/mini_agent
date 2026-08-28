# Conversation Runtime

该包实现 Session/Turn 的应用服务和用户输入生命周期。

- `__init__.py`：Conversation 子包标记；公开能力由 `service.py` 与 `ports.py` 定义。
- `service.py`：`ConversationService` 门面，启动 child、运行、恢复和 handoff。
- `ports.py`：存储/事件依赖协议；`bridge_support.py`：Node bridge 与 compaction 支持。
- `session_control.py`、`user_input.py`、`steering.py`：Session 控制、消息输入和 FIFO steering。
- `references.py`：文件引用规范化；`recovery/`：恢复预览与 resume。

对外通过 `ConversationService`/ports；不直接依赖 FastAPI，Turn 树写入交由 node bridge/storage。
