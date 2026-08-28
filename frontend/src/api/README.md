# 前端 API 门面

本目录是浏览器访问本地 backend 的唯一协议层。`index.ts` 汇总各领域导出，`settings.ts` 管理用户、Agent、Runtime、Sandbox 与 Provider 设置，`benchmarks.ts` 管理工具/Skill/任务查询和 benchmark 执行。

- `transport/`：URL、JSON 请求、错误映射等 HTTP 基础设施。
- `conversations/`：Session、SidebarThread、Turn 与 SSE 聊天协议。
- `projects/`：项目和 Session 文件资源。
- `runtime/`：后台 Job 查询与取消。

组件应优先从 `../api` 聚合入口导入，不自行拼接 HTTP/SSE。该层依赖共享 `../types.ts`，不依赖页面状态。
