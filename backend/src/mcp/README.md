# MCP 集成

该包同时提供本地只读 MCP server 和审批型外部 MCP client。

- `__init__.py`：公开 MCP 配置计划、本地 Tool adapter 和 server 工厂。
- `server.py`：`McpToolAdapter` 与 `create_server`。
- `config.py`：`McpServerConfig`、`McpConfigPlan`、信任与 secret-reference 解析。
- `controlled_stdio.py`：受 Job/Sandbox 控制的 stdio 子进程生命周期。
- `cli.py`：本地 stdio server 命令入口；`client/`：外部 server 管理与 Tool 适配。

配置和输出一律视为不可信；明文 secret、无限输出和绕过审批的调用均不允许。

MCP 生命周期和 Tool 包装异常仅承载控制分类；向 HTTP、Tool、Turn 或审计输出的文本必须使用统一脱敏后的根因原消息。
