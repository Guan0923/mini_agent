# 本地持久化

该包保存 Session、SidebarThread、Turn、审批、项目和设置。

- `__init__.py`：公开 SQLite 与 Redis message queue 存储入口。
- `message_queue.py`：版本化 Redis queued-message、Turn Stream、consumer group、ack/reconciliation 与可注入测试端口。
- `sqlite.py`：`SQLiteSessionStore` 聚合各 SQLite mixin。
- `sqlite_base.py`、`sqlite_schema.py`：连接/事务基础与 schema。
- `sqlite_sessions.py`、`sqlite_sidebar_threads.py`、`sqlite_approvals.py`：领域表操作。
- `sqlite_runtime/`：Runtime Node、Checkpoint 和 JSON record。
- `projects.py`：`ProjectStore`；`settings/`：TOML/SQLite 设置与凭据加密。
- `codec.py`：RuntimeState/消息 JSON 编解码。

存储层不发布 UI 事件；事务边界由 repository 明确控制，secret 不得写入 `config.toml`。
