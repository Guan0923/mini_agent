# Job Registry

该包分离 Job 注册、准入、Scope 与终态历史。

- `core.py`：`JobRegistry` 门面和当前 Job 索引。
- `admission.py`：lane/slot 准入与排队调度。
- `lifecycle.py`：取消、终态、历史裁剪。
- `models.py`：公开查询模型；`scopes.py`：owner/scope 匹配。
- `__init__.py`：保持 `backend.jobs.registry` 导入入口。

Registry 是 Job 状态唯一协调者；业务模块通过 Scope 查询，不能直接修改内部映射。
