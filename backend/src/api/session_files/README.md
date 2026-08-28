# Session 文件 API

该包管理项目文件引用与 Session upload。

- `store.py`：`SessionFileStore`、`SessionFileError`，负责 workspace/upload 路径边界、搜索和删除。
- `routes.py`：multipart 上传、搜索、内容 GET/HEAD 和删除端点。
- `__init__.py`：router 公开入口。

禁止跟随越界 symlink；响应受大小/MIME 限制，上传删除只作用于目标 Session。
