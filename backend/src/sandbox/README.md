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

`run_command` 的 Windows 文件隔离使用每-job随机普通 `S-1-5-21-*` Capability SID，而不是
AppContainer。Token 使用固定低权限账户、Capability、Account、Logon 与 Everyone SID，
不启用 `WRITE_RESTRICTED`。Backend 只解析并审计本次命令明确声明的 workspace、cwd 和
Job temp，对这些路径添加可验证、可精确撤销的 ACL lease；不会扫描用户目录、PATH、
Windows 目录或固定磁盘，也不向执行边界外的路径写临时 Deny ACE。

系统其他位置能否写入由低权限账户原有 DACL 决定；这里不声称对整台 Windows 提供
“绝对不可写”证明。
