# Subagent 运行协作

该包把原 `runtime.subagents` 中的执行、工具、报告和父 Runtime 桥接职责拆开。`runtime/subagents.py` 仍是兼容门面和 `SubagentCoordinator` 组合入口；本包的内部 mixin 不单独构造，也不绕过 Coordinator 的单层/并发限制。

- `contracts.py`：定义可注入的 `ChildRunner`、`AgentThreadEvents` Protocol，以及 Coordinator 内部使用的 Session、状态和 canonical store 适配器。
- `execution.py`：`_SubagentExecutionMixin` 创建子 Thread/首个 Turn、调度 worker、恢复既有子任务并管理终态。
- `tool_actions.py`：`_SubagentToolActionsMixin` 实现 `delegate_tasks`、`send_agent_message`、`wait_tasks`、`close_agent` 等工具动作和目标解析。
- `reports.py`：`_SubagentReportDeliveryMixin` 负责 Redis 报告 claim、SQLite 幂等持久化、父线程通知和 ACK 顺序。
- `parent_bridge.py`：`ParentRuntimeBridge` 将子 Runtime 的事件、审批与用户输入串行转发到父 Runtime；`BridgeEvent`、`BridgeApproval` 是桥接载荷。
- `tool_executor.py`：`WorkspaceWriteLock` 对重叠路径加锁，`LockedToolExecutor` 在执行 workspace 写工具和命令时应用该锁。
- `__init__.py`：只公开 `AgentThreadEvents`、`ChildRunner`、`ParentRuntimeBridge`、`WorkspaceWriteLock` 和 `LockedToolExecutor`。

主要依赖方向为 `runtime.subagents -> runtime.subagent -> domain/storage/runtime ports`。显式 Agent 消息通过持久化 mailbox 传递；SSE 只负责界面观察。消息必须先成功写入 SQLite，再 ACK Redis，Redis 不可用时保持 fail closed。
