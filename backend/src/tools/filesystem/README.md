# Workspace Filesystem

该包实现 workspace 和 project 边界内的文件操作。

文件工具接受 `workspace:相对路径`、`project:相对路径` 和允许目录内的绝对路径。
裸路径在项目对话中指向 project，否则指向 workspace；找不到时直接报错，不切换目录。
输出统一使用带前缀的相对路径，两目录外的只读 Skill 路径保持绝对路径。
路径解析与写入锁使用 `domain/file_paths.py` 的同一套规则；这些前缀不用于 shell 命令文本。

- `workspace.py`：`WorkspaceFiles` 门面和根路径策略。
- `paths.py`：resolve、symlink 与边界验证。
- `reading.py`、`writing.py`、`io.py`：有界读取、原子写入和目录操作。
- `__init__.py`：公开 filesystem Handler 能力。

所有路径先 resolve 后验证 workspace；禁止借 symlink/绝对路径越界，输出必须受限。
