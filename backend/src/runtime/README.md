# Runtime 编排

该包执行 Planner 决策、工具调用、Turn 生命周期和恢复。

- `__init__.py`：通过惰性导出提供 Runtime 的稳定公共门面，避免包入口重新形成应用级导入环。
- `application/`：依赖装配；`core/`：Runtime 契约、事件、Hook 和上下文。
- `execution/`：runner、step、workflow、取消与终态。
- `conversation/`：Session/Turn 服务、输入、steering、恢复与引用。
- `node_bridge/`：Runtime 事件到持久化 Node/Item 的桥接。
- `persistence/`、`planning/`：checkpoint/recording 与 Plan mode。
- `capability_settings.py`：`SkillSettings`、`SubagentSettings` 解析并校验可选能力配置。
- `subagent_bridge.py`：`ParentRuntimeBridge` 在父运行线程上串行转发子 Agent 事件和审批。
- `subagent_tools.py`：`WorkspaceWriteLock` 与 `LockedToolExecutor` 隔离并发子 Agent 的同路径写入和命令执行。
- 根文件 `executor.py`、`state_tree.py`、`job_events.py`、`subagents.py` 提供 Runtime 组合入口和专用桥接。

Runtime 发布事件而不渲染；跨层依赖通过 ports/contracts，Sandbox 不可用时必须 fail closed。
