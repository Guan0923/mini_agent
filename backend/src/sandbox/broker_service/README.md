# Windows Broker Service

该包实现高权限 Windows Service 的服务端协议和账户池。

- `configuration.py`：`BrokerConfiguration`；`protocol.py`：签名 canonical payload 与文件协议。
- `credentials.py`：DPAPI key store；`accounts.py`：`AccountPool`/lease。
- `installer.py`：`WindowsServiceInstaller`；`pipe.py`：`WindowsNamedPipeServer`。
- `service.py`：`WindowsBrokerService` 请求处理。
- `__init__.py`：服务端公开接口。

命名管道必须验证签名、SID 和请求 schema；账户租约必须在所有终态释放。
