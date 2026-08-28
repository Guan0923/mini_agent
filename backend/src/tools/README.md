# Tool 系统

该包定义 Tool 契约、注册表和本地工具实现。

- `__init__.py`：汇总公开 Tool 契约、registry、命令/文件/Web 实现和默认目录工厂。
- `base.py`：`Tool`、`ToolInvocationContext`、`ToolError`、执行协议。
- `registry.py`、`catalog.py`：Tool 注册和默认目录装配。
- `command.py`、`terminal.py`：workspace 命令与终端选择。
- `delegation.py`：单层 subagent 工具；`default_tools/`：公开 Tool schema/handler。
- `filesystem/`、`web/`：路径边界和 SSRF 安全实现。

所有 tool_call 在 Handler 前经过 Runtime Hook/Sandbox 决策；资源语义校验仍必须保留在具体 Handler。
