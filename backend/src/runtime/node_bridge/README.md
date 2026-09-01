# Runtime Node Bridge

该包把 Runtime 事件投影成持久化 Turn/Item，并保持流式状态一致。

- `core.py`：`RuntimeEventNodeBridge` 门面组合各 mixin。
- `lifecycle.py`：Turn 启动与 binding；`items.py`：文本/tool/thinking Item 流。
- `events.py`：Runtime event 映射；`finalization.py`：Item/Turn 终态。
- `__init__.py`：保持 `runtime.node_bridge` 公开入口。

Bridge 是 Runtime 事件与 `domain.runtime_state` 的唯一翻译层；partial pause Item 必须保留并标记 failed。

Provider 的 `model_retry` 在活动 Turn 内投影为持久化的非终态 `retry` Item：每次失败立即发送 delta，下一次 `model_request` 将上一项结算为 success，Turn 提前终结则由统一 Item 终结逻辑结算。Retry 只记录脱敏根因消息、次数与延迟，不携带可见的 HTTP/request id 控制元数据，也不算作模型已经产生部分输出。
