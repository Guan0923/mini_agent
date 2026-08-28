# API 共享装配

该包保存多个 router 共用、但仍属于 API 边界的组合逻辑。

- `__init__.py`：公开 `build_local_application` 这一共享应用装配入口。
- `runtime.py`：`build_local_application` 组合 Runtime、Tool、MCP、Sandbox 与项目 Session provisioner。
- `info.py`：Tool、Skill 与本地路径只读查询。
- `benchmark.py`：benchmark 请求模型、执行端点和独立 app 工厂。

这里可以依赖 WebAppState 和 Runtime factory，但不得成为通用杂项目录；新增内容必须确实被多个 API 领域共享。
