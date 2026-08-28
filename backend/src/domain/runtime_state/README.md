# Runtime State Domain

该包定义持久化 RuntimeRoot/Turn 树与 Item/Frame 契约。

- `contract.py`：schema 校验、`message_payload`、`turn_payload`、canonical Item status。
- `models.py`：`RuntimeRootState`、`RuntimeState` 和 dict 转换。
- `tree.py`：`RuntimeStateTree` 与 `InMemoryNodeStore`。
- `frames.py`：`NodeFrame` 和 snapshot/delta 生成。
- `writer.py`：`NodeWriter` 的消息、Item、终态和 checkpoint 写入。
- `__init__.py`：保持 `backend.domain.runtime_state` 公开入口。

Root 只作锚点；UI/模型投影不得包含 Root。`Message[][]` 和 running/failed/success Item 状态是跨层协议。
