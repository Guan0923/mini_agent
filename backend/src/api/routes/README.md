# 资源路由

该包按 HTTP 资源拆分非流式及 Turn 控制路由。

- `turns.py`：仅保留 Turn list/create/rewind/resume/pause/steer/fork/compact/config HTTP endpoint 和依赖调用。
- `turn_models.py`：Turn create/rewind/fork/current-data/config 的 Pydantic 请求模型；`TurnExecutionConfig` 仍由 `turns.py` 重导出供 Agent Thread 路由复用。
- `turn_support.py`：集中 Session/Thread/Turn 查找、queued delivery 规范化、Runtime 错误到 HTTP 的映射，以及 SSE 启动/恢复辅助函数。
- `sidebar_threads.py`：Thread 列表和状态变更。
- `projects.py`、`settings.py`：项目/Skill 信任与本地设置。
- `jobs.py`、`runtime_nodes.py`：Job 和 Runtime Node 查询。
- `sandbox.py`：Broker status/install/repair。
- `__init__.py`：routers 汇总。

路由只验证请求与映射异常；数据一致性由 storage/runtime 服务负责。
