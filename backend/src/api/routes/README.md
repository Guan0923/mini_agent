# 资源路由

该包按 HTTP 资源拆分非流式及 Turn 控制路由。

- `turns.py`：Turn list/create/rewind/resume/pause/steer/fork/compact/config。
- `sidebar_threads.py`：Thread 列表和状态变更。
- `projects.py`、`settings.py`：项目/Skill 信任与本地设置。
- `jobs.py`、`runtime_nodes.py`：Job 和 Runtime Node 查询。
- `sandbox.py`：Broker status/install/repair。
- `__init__.py`：routers 汇总。

路由只验证请求与映射异常；数据一致性由 storage/runtime 服务负责。
