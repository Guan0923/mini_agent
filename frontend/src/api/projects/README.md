# Project 与文件 API

该包封装项目索引和 Session 文件资源。

- `projects.ts`：`listProjects`、`createProject`、`createProjectSession`、路径变更、移除/恢复与 Skill 信任管理。
- `files.ts`：上传、搜索、删除和 `sessionFileContentUrl`，并保留上传进度回调与 `ApiError` 映射。
- `index.ts`：领域公开入口，继续支持 `api/projects` 导入。

文件内容 URL 只由 `files.ts` 生成；组件不得绕过它手工构造引用地址。
