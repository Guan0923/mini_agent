# Runtime Job API

该包承载后台 Job 控制面。

- `jobs.ts`：定义 `JobInfo`，提供 `listJobs`、`getJob`、`cancelJob`。
- `index.ts`：对 API 根门面导出 Job 能力。

它依赖 `../transport/request.ts`，不负责 Turn SSE 或前端运行状态投影。
