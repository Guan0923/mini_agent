# 默认 Tool 定义

该包把 Handler 能力包装为模型可见的 Tool schema。

- `filesystem.py`：读、写、列目录和 upload 读取 Tool。
- `command.py`：`run_command`；`web.py`：search/fetch；`time.py`：当前时间。
- `todo.py`：`update_todo_list`；`schema.py`：`object_schema`。
- `__init__.py`：`build_default_tools` 汇总只读/可写集合。

schema 与 Handler 参数必须同步；审批不能替代具体 Handler 的安全检查。
