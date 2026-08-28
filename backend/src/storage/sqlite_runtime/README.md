# SQLite Runtime Repository

该包保存 Runtime Node、Checkpoint 与 JSON record 原语。

- `nodes.py`：RuntimeRoot/Turn 创建、更新、列表和 active/running 查询。
- `checkpoints.py`：checkpoint 保存与恢复。
- `records.py`：JSON 对象的统一编解码/持久化辅助。
- `__init__.py`：组合 mixin 并保持 `storage.sqlite_runtime` 导入入口。

所有写入使用调用方连接/事务边界；schema 校验由 domain runtime_state 执行。
