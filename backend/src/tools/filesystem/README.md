# Workspace Filesystem

该包实现 workspace 边界内的文件操作。

- `workspace.py`：`WorkspaceFiles` 门面和根路径策略。
- `paths.py`：resolve、symlink 与边界验证。
- `reading.py`、`writing.py`、`io.py`：有界读取、原子写入和目录操作。
- `__init__.py`：公开 filesystem Handler 能力。

所有路径先 resolve 后验证 workspace；禁止借 symlink/绝对路径越界，输出必须受限。
