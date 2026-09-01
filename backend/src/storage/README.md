# 本地持久化

该包保存 Session、SidebarThread、Turn、审批、项目和设置。

- `__init__.py`：公开 SQLite 与 Redis message queue 存储入口。
- `message_queue.py`：稳定导出门面，保持 `MemoryMessageQueue`、`RedisMessageQueue` 和 mailbox 的既有导入路径。
- `message_queue_support.py`：队列 key/TTL/consumer group 常量，以及 fingerprint、queued message 合并和幂等比较。
- `redis_message_queue.py`：`RedisMessageQueue` 实现可编辑 queue、Redis Streams claim/recovery、delivery receipt 和 ACK；`redis_message_scripts.py` 单独保存四个原子 Lua 协议。
- `memory_message_queue.py`：`MemoryMessageQueue` 提供同一协议的线程安全内存实现，用于本地测试和显式注入。
- `message_mailbox.py`：`RedisTurnMailbox`/`RedisAgentMailbox` 将 queue 适配为 Runtime 与 Subagent mailbox port。
- `sqlite.py`：`SQLiteSessionStore` 聚合各 SQLite mixin。
- `sqlite_base.py`、`sqlite_schema.py`：连接/事务基础与 schema。
- `sqlite_json.py`：`read_json_object` 提供 Session 与 Runtime mixin 共用的单对象读取和 JSON object 校验。
- `sqlite_sessions.py`、`sqlite_sidebar_threads.py`、`sqlite_approvals.py`：领域表操作。
- `sqlite_runtime/`：Runtime Node、Checkpoint 和 JSON record。
- `projects.py`：`ProjectStore`；`settings/`：TOML/SQLite 设置与凭据加密。
- `codec.py`：RuntimeState/消息 JSON 编解码。

存储层不发布 UI 事件；Redis 投递是 FIFO at-least-once，SQLite 幂等持久化成功后才 ACK。事务边界由 repository 明确控制，secret 不得写入 `config.toml`。

Storage 包装类型只保留队列/事务等控制语义；任何向 HTTP、Runtime、Job 或审计传播的错误文本都必须统一投影并脱敏最底层异常消息。
