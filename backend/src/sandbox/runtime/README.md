# Sandbox Runtime Plane

该包执行已批准的 Sandbox 决策并管理资源生命周期。

- `admission.py`：运行准入；`launcher.py`：`SandboxLauncher`；`audit.py`：外部可写路径有界审计。
- `leases.py`：Capability allow/deny ACE、路径身份与 TEMP 的原子 backend lease。
- `manifest.py`：命令/环境 manifest；`resources.py`：资源租约。
- `monitor.py`：限制监控；`reclaimer.py`：终态回收。
- `__init__.py`：惰性导出，避免 control/runtime 循环加载。

Launcher 只消费明确决策；三种文件模式都经 Broker 和固定低权账户，full_access 仅不启用
`WRITE_RESTRICTED`。MCP、Web 和其他内置工具不使用这里的 Broker 生命周期。
