# Backend 源码

backend 是本地单用户 Runtime 与 HTTP/SSE 服务。`configuration.py` 提供 `ClientPaths`、`LocalConfigStore` 和原子 TOML 写入；其余能力按领域分包。

- `__init__.py`：公开版本号，并惰性导出 `AgentRunner`，避免导入 backend 时加载完整应用图。
- `domain/`：无外层依赖的消息、计划、Session、Skill 与 RuntimeState 契约。
- `planning/`、`runtime/`、`providers/`、`tools/`：模型决策、执行编排、Provider 与工具。
- `api/`：FastAPI/HTTP/SSE 边界。
- `storage/`：Redis message queue、SQLite、本地设置与项目数据。
- `sandbox/`、`jobs/`、`mcp/`、`skills/`：隔离、作业、外部 MCP 和 Skill 信任。
- `observability/`：结构化事件与 JSONL。

依赖必须向内；domain 不得导入 API/storage，provider 不得导入 storage，Sandbox Broker 不得静默降级。

## 错误投影约束

内部异常类型只承载 retryable、HTTP status、Broker code、Tool 分类等控制元数据。HTTP、SSE、Turn、Trace、审计与前端展示统一沿 `__cause__`/`__context__` 投影最底层异常的 `str()`；空消息才使用类名。所有外部错误文本必须经过统一的 credential 脱敏，包装异常不得添加展示前缀。
