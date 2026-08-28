# Process Jobs

该包实现受 Job Registry 管理的子进程。

- `group.py`：跨平台进程组创建、终止与树清理。
- `job.py`：`SubprocessJob` 的启动、输出、取消和终态。
- `__init__.py`：公开进程 Job 接口。

进程生命周期必须通过 Job Object/进程组收口；调用方不得只杀父 PID。
