# Sandbox Runtime Plane

该包执行已批准的 Sandbox 决策并管理资源生命周期。

- `admission.py`：运行准入；`launcher.py`：`SandboxLauncher`。
- `manifest.py`：命令/环境 manifest；`resources.py`：资源租约。
- `monitor.py`：限制监控；`reclaimer.py`：终态回收。
- `__init__.py`：惰性导出，避免 control/runtime 循环加载。

Launcher 只消费明确决策；read_only/workspace_write 必须经 Broker，full_access 仍受 Job Object 控制。
