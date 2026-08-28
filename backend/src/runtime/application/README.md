# Runtime Application

该包是 Runtime 的依赖装配层。

- `factory.py`：构建 `ConversationService`、Planner、Provider、Tool、MCP、Sandbox 与 storage 适配。
- `services.py`：应用级 service 集合和生命周期。
- `__init__.py`：公开工厂与 service 类型。

只有此层可以知道多数具体实现；domain/core 通过 contracts/ports 被注入，避免反向依赖。
