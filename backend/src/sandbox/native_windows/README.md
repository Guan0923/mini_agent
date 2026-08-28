# Windows Sandbox 原语

该包封装 Windows 专用账户、Token、Job Object、ACL 与 WFP。

- `api.py`：平台检查和延迟加载 Win32 模块。
- `accounts.py`：`WindowsAccountManager`、受限 Token。
- `jobs.py`：`WindowsJobObject`；`security.py`：`WindowsAclManager` 和 pipe security descriptor。
- `network.py`：`WindowsPowerShellWfpController` 与允许地址/端口补集。
- `__init__.py`：低层公开入口。

这些原语不包含业务审批；非 Windows 平台调用必须显式失败。
