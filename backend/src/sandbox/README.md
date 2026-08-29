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
AppContainer。`read_only`/`workspace_write` Token 保留 `WRITE_RESTRICTED` 并把 `Everyone`
加入 restricting SID 以支持 Winsock/loopback；`Everyone` 不进入 Token 默认 DACL。
为防止 workspace 外已有的 `Everyone`/sandbox account 可写 ACL 绕过限制，backend 会在
启动前执行一层、每目录 1000 项、总计 50000 项、2 秒的有界扫描，并为本次 Capability
添加 deny-write ACE。扫描、ACL读取、路径身份或 deny 应用不完整时命令 fail closed。

这是有界扫描下的实用隔离，不是对全系统所有路径的形式化不可写证明。风险边界与
`Everyone` 可写目录警告参见 [OpenAI Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)。
