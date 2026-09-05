# Session 文件 API

该包管理 workspace、project 文件引用与 Session upload。

接口返回的 `path` 和 `display_path` 都使用 `workspace:相对路径` 或 `project:相对路径`。
`source` 分为 `workspace`、`project`、`upload`；上传仍存放在 `workspace:uploads/`，只有上传来源可通过删除接口删除。
搜索覆盖两个完整目录，支持前缀筛选；无项目对话不提供 project 来源。

- `store.py`：`SessionFileStore`、`SessionFileError`，负责 workspace/upload 路径边界、搜索和删除。
- `routes.py`：multipart 上传、搜索、内容 GET/HEAD 和删除端点。
- `__init__.py`：router 公开入口。

禁止跟随越界 symlink；响应受大小/MIME 限制，上传删除只作用于目标 Session。
