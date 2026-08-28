# Chat HTTP/SSE

该包处理 Turn 创建/附着流和交互决策的稳定门面。

- `routes.py`：路由门面与可替换的 `build_local_application` 注入点。
- `models.py`：Runtime 模型请求与快照转换。
- `streaming.py`：SSE 启动、锁、终态和异常映射。
- `titles.py`：首条主 Thread 消息标题策略。
- `decisions.py`、`interrupts.py`：决策提交和 `DecisionRegistry`。
- `__init__.py`：router 公开入口。

HTTP/SSE payload 语义保持稳定；模型/Runtime 业务通过 shared runtime 和 WebAppState 调用。
