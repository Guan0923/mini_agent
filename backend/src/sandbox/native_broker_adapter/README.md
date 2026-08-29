# Native Broker Adapter

该包把 Broker manifest 转换为 Windows 原生隔离进程。

- `adapter.py`：`WindowsNativeBrokerAdapter`、Capability reservation、资源 provider 与 lease。
- `process.py`：`_NativeWindowsProcess` 的 stdio/终止适配。
- `__init__.py`：保持 `sandbox.native_broker_adapter` 入口。

Adapter 依赖 `native_windows`，为每个 reservation 持有 Token 和 private desktop，并校验
policy hash 与 Capability digest；它不负责用户审批、HTTP代理或 workspace ACL。
