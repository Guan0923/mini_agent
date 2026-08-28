# Domain 契约

该包定义不依赖外层实现的核心数据模型。

- `__init__.py`：汇总并稳定导出消息、Session、运行状态、Skill 与 RuntimeState 公共契约。
- `messages.py`：System/User/Assistant/Tool 消息、`ToolSpec` 和序列化。
- `plans.py`：`AgentAction` 与计划结构；`state.py`：`RunState`、`RunHandoff`、checkpoint/provenance。
- `session.py`、`sidebar_thread.py`：Session、恢复预览和 SidebarThread。
- `skills.py`：`SkillSnapshot`、`SkillSelection`；`terminal.py`：终端类型归一化。
- `state_codec.py`：RunState 编解码；`errors.py`：规划/模型输出异常。
- `runtime_state/`：持久化 Turn 树和 `Message[][]` 契约。

外层通过这些类交换数据；本包不得导入 FastAPI、SQLite、Provider 或 UI。
