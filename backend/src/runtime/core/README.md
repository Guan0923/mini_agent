# Runtime Core

该包定义执行核心的配置、事件、Hook 与依赖端口。

- `contracts.py`、`ports.py`：Planner、Tool、存储和事件协议。
- `config.py`：运行配置；`events.py`：Runtime event 类型。
- `hook_contracts.py`：不可变 Hook context、outcome 与 operation result；`hooks.py`：生命周期 manager 与 `sandbox_operation` 注册。
- `context/`：`AgentRuntime`、`RuntimeState`、`RuntimeExchange`。
- `__init__.py`：核心公开接口。

Core 依赖 domain 抽象，不依赖 API 展示；具体服务由 application factory 注入。
