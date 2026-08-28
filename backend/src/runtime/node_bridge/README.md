# Runtime Node Bridge

该包把 Runtime 事件投影成持久化 Turn/Item，并保持流式状态一致。

- `core.py`：`RuntimeEventNodeBridge` 门面组合各 mixin。
- `lifecycle.py`：Turn 启动与 binding；`items.py`：文本/tool/thinking Item 流。
- `events.py`：Runtime event 映射；`finalization.py`：Item/Turn 终态。
- `__init__.py`：保持 `runtime.node_bridge` 公开入口。

Bridge 是 Runtime 事件与 `domain.runtime_state` 的唯一翻译层；partial pause Item 必须保留并标记 failed。
