# 本地 Web API

该包装配 FastAPI 应用并维护本地 HTTP/SSE 生命周期。

- `__init__.py`：API 包标记，不提前装配应用或产生启动副作用。
- `app.py`：`create_app` 注册 chat、routes、session_files 与 shared 路由，通过 FastAPI lifespan 关闭 `WebAppState`，并在 `frontend/dist` 存在时从同一 loopback Origin 托管生产前端。
- `__main__.py`：`main` 以 `127.0.0.1:8000` 启动 `create_app()`，是 `python -m backend.api` 的命令入口。
- `state.py`：`WebAppState` 组合 storage、Runtime、Job、Sandbox 和决策注册表。
- `active_turn_stream.py`：`ActiveTurnStream`/`ActiveTurnSubscription` 复用 running Turn SSE。
- `pause_control.py`：进程内 `TurnPauseController`；Turn steering 由 Redis mailbox 承载。
- `security.py`：loopback Origin 校验；`session_store.py`、`user_data.py` 管理 Session 数据边界。
- `chat/`、`routes/`、`session_files/`、`shared/`：按协议领域拆分的路由。

公开入口是 `create_app` 与 `python -m backend.api`。API 层只做协议转换和编排，不承载 Provider/Storage 业务规则。
