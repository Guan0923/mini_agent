# Tool 系统

该包定义 Tool 契约、注册表和本地工具实现。

- `__init__.py`：汇总公开 Tool 契约、registry、命令/文件/Web 实现和默认目录工厂。
- `base.py`：`Tool`、`ToolInvocationContext`、`ToolError`、执行协议。
- `registry.py`：只维护 Tool 注册、schema 校验和调用查找；workspace 构造兼容入口延迟调用 `workspace_tools`，不反向依赖 catalog。
- `workspace_tools.py`：`build_workspace_tools` 构造一个 workspace 的具体默认 Tool 集合。
- `catalog.py`：`build_tool_registry` 把具体 Tool 集合与额外 Tool 装入 `ToolRegistry`，是应用装配入口。
- `command.py`、`terminal.py`：workspace 命令与终端选择。
- `delegation.py`：单层 subagent 工具；`default_tools/`：公开 Tool schema/handler。
- `filesystem/`、`web/`：路径边界和 SSRF 安全实现。

所有 tool_call 在 Handler 前经过 Runtime Hook/Sandbox 决策；资源语义校验仍必须保留在具体 Handler。

`ToolError` 可以保留工具失败分类，但捕获底层异常时不得添加工具名或 unexpected 前缀；Tool result/error 展示统一使用脱敏后的根因原消息。
