# Sandbox

该包实现工具调用审批、运行准入和 Windows Broker 隔离。

- `__init__.py`：汇总公开审批、策略、Launcher、Broker service 和 Windows 原生适配类型。
- `policy.py`、`errors.py`：权限模式、资源/安装失败契约。
- `control/`：每次 tool_call 的审批、授权和执行决策。
- `runtime/`：Launcher、Manifest、监控、资源和回收。
- `broker_service/`：Windows Service 与 named pipe 服务端。
- `native_windows/`、`native_broker_adapter/`：OS 原语与 Broker 进程适配。
- `install_helper.py`、`service_main.py`、`windows_broker.py`：安装/修复和服务入口。

`full_access` 与 Broker 模式必须显式区分；任何健康或安装失败都不得静默回退为后端用户执行。
