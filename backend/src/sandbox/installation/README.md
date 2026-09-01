# Broker 安装事务组件

该包保存提升权限的 Windows Broker 安装事务所需的纯合约、账户策略和 ACL 操作。外部进程入口仍是 `backend.sandbox.install_helper.main`，以保持命令行、exit code 和测试 patch 路径稳定。

- `contracts.py`：集中 `EXIT_*` 进程码、`TransactionFailure`、固定 Broker service class，以及 `validate_payload` 对不可信 CLI JSON 的路径、端口和服务命令校验。
- `accounts.py`：`provision_fixed_accounts` 创建或验证 `MiniSbxOffline`/`MiniSbxOnline` 与受管本地组，轮换并 DPAPI 持久化凭据，授予最小登录权；`remove_owned_accounts` 只删除可由凭据包和 SID 证明归属的账户；其余 helper 负责凭据探测和冲突检查。
- `access_policy.py`：构造 ProgramData、敏感文件、source/runtime path 的 ACL 命令；`_SourceAclGrant` 描述精确 grant，`_iter_acl_tree` 在不跟随 reparse point 的前提下枚举声明树，`_secure_source_code` 和 `_apply_source_acl_grant` 对目录、既有子项及未来继承执行幂等的 Broker SID 只读/执行授权。
- `__init__.py`：仅重导出 installer 现有调用方需要的 ACL helper。

`install_helper.py` 负责 SCM 事务顺序和稳定门面，并通过显式 callback 把网络配置注入账户 provisioning，避免账户模块反向依赖事务入口。Reinstall 会对 source/runtime 树中仅属于 Broker 服务 SID 的 ACE 做对称清理。路径校验必须发生在任何提升权限写操作之前；账户归属、ACL 或 WFP 状态无法证明时均失败关闭。
