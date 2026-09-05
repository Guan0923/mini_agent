# External MCP Client

该包管理外部 MCP server 生命周期并适配成内部 Tool。

- `manager.py`：`ExternalMcpManager`、service driver 和 `ExternalMcpResources`。
- `lifecycle.py`：配置加载、启动、热替换与关闭。
- `adapters.py`：stdio 参数、环境 secret-reference 和结果渲染。
- `transports.py`：stdio / Streamable HTTP，凭据在连接时解析。
- `features.py`：资源、模板、提示词的审批型 Agent 工具。
- `subscriptions.py`：本轮订阅、更新合并、断线与溢出状态。
- `__init__.py`：公开 Manager 与生命周期函数，并保留测试注入入口。

所有外部输出视为不可信；stdio 进程必须由 controlled stdio/Job/Sandbox 管理。
