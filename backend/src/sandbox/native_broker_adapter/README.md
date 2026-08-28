# Native Broker Adapter

该包把 Broker manifest 转换为 Windows 原生隔离进程。

- `adapter.py`：`WindowsNativeBrokerAdapter`、资源 provider 与 lease。
- `process.py`：`_NativeWindowsProcess` 的 stdio/终止适配。
- `protocol.py`：WFP controller 协议。
- `__init__.py`：保持 `sandbox.native_broker_adapter` 入口。

Adapter 依赖 `native_windows`，不负责用户审批或 HTTP；失败必须携带可映射的 Sandbox 错误。
