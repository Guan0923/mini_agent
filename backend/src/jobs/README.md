# Job 控制面

该包统一后台 Thread、Process 与 Service 作业的生命周期和准入。

- `__init__.py`：统一导出 Job 基类、调度/作用域类型、registry 以及进程和服务实现，不加载 Runtime/API。
- `base.py`：Job 抽象；`thread_job.py`、`service_job.py`：线程和长期服务实现。
- `scheduling.py`：`JobLane`、`AdmissionPolicy`、slot/queue 限制。
- `scope.py`：`JobOwner`、`JobScope`；`output.py`：有界输出。
- `safety.py`、`errors.py`：错误格式和失败类型；`registry/`：注册、查询、取消与历史。
- `processes/`：进程组和子进程 Job。

`jobs.__init__` 是公开入口。Runtime/MCP/Sandbox 通过 registry 使用 Job，不直接共享可变进程表。
