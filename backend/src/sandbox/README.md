# Sandbox

该包实现工具调用审批、运行准入和 Windows Broker 隔离。

- `__init__.py`：汇总公开审批、策略、Launcher、Broker service 和 Windows 原生适配类型。
- `policy.py`、`errors.py`：权限模式、资源/安装失败契约。
- `control/`：每次 tool_call 的审批、授权和执行决策。
- `runtime/`：Launcher、Manifest、监控、资源和回收。
- `broker_service/`：Windows Service 与 named pipe 服务端。
- `native_windows/`、`native_broker_adapter/`：OS 原语与 Broker 进程适配。
- `install_helper.py`：提升权限安装/repair/reinstall 的稳定 CLI 门面和 SCM 事务顺序。
- `installation/`：payload/exit-code 合约、固定账户与凭据生命周期、source/runtime ACL 策略。
- `service_main.py`、`windows_broker.py`：服务和 Broker 入口。

`full_access` 与 Broker 模式必须显式区分；任何健康或安装失败都不得静默回退为后端用户执行。

`run_command` 的 Windows 文件隔离使用每-job随机普通 `S-1-5-21-*` Capability SID，而不是
AppContainer。Token 使用固定低权限账户、Capability、Account、Logon 与 Everyone SID，
并使用 `WRITE_RESTRICTED` 将 RestrictedSids 检查限定为写访问。Backend 只解析并审计本次命令明确声明的 workspace、cwd 和
Job temp；对这些路径添加可验证、可精确撤销的 ACL lease，并对通向这些路径的明确父目录链添加无继承、只包含
`FILE_TRAVERSE | FILE_READ_ATTRIBUTES` 的 Account SID 通行 lease。父目录 lease 通过目录句柄直接更新，不向子目录传播。
系统不会扫描用户目录、PATH、Windows 目录或固定磁盘，也不向执行边界外的路径写临时 Deny ACE。

Broker readiness 使用 `token_model=capability_sid_v3`；旧 Token 模型必须 repair，不允许协议降级。

系统其他位置能否写入由低权限账户原有 DACL 决定；这里不声称对整台 Windows 提供
“绝对不可写”证明。

Sandbox/Broker failure code 与异常类型继续作为控制元数据；状态、API、Turn 和审计中的可见 `detail`/`message` 只使用统一脱敏后的根因原消息。
