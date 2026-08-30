# Sandbox Runtime Plane

该包执行已批准的 Sandbox 决策并管理资源生命周期。

- `admission.py`：运行准入；`launcher.py`：`SandboxLauncher` 与显式路径审计。
- `leases.py`：workspace/cwd/temp 的精确 ACE ownership 与原子 backend lease。
- `manifest.py`：命令/环境 manifest；`resources.py`：资源租约。
- `monitor.py`：限制监控；`reclaimer.py`：终态回收。
- `__init__.py`：惰性导出，避免 control/runtime 循环加载。

Launcher 只消费明确决策；三种文件模式都经 Broker 和固定低权账户；`read_only` 和
`workspace_write` 使用 `WRITE_RESTRICTED` 将 RestrictedSids 检查限定为写访问。
workspace、cwd 与 Job temp 的明确父目录链只获得 Account SID 的无继承遍历 lease，
以便受限 PowerShell 建立真实工作目录；这些 ACE 与其他 Job ACL 一起精确回滚。
MCP、Web 和其他内置工具不使用这里的 Broker 生命周期。
