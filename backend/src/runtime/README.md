# Runtime 编排

该包执行 Planner 决策、工具调用、Turn 生命周期和恢复。

- `__init__.py`：通过惰性导出提供 Runtime 的稳定公共门面，避免包入口重新形成应用级导入环。
- `application/`：依赖装配；`core/`：Runtime 契约、事件、Hook 和上下文。
- `execution/`：runner、step、workflow、取消与终态。
- `conversation/`：Session/Turn 服务、输入、steering、恢复与引用。
- `node_bridge/`：Runtime 事件到持久化 Node/Item 的桥接。
- `persistence/`、`planning/`：checkpoint/recording 与 Plan mode。
- `capability_settings.py`：`SkillSettings`、`SubagentSettings` 解析并校验可选能力配置。
- `subagent/`：按 contracts、执行、报告投递、父 Runtime 桥接和工具锁拆分子 Agent 协作；其 README 记录 Redis/SQLite ACK 顺序。
- 根文件 `executor.py`、`state_tree.py`、`job_events.py` 提供 Runtime 组合入口；`subagents.py` 保留 `SubagentCoordinator` 稳定门面并组合 `subagent/` mixin。

Runtime 发布事件而不渲染；跨层依赖通过 ports/contracts，Sandbox 不可用时必须 fail closed。
