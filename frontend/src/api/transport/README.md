# HTTP Transport

该包集中处理 loopback HTTP 请求，不包含业务状态。

- `base.ts`：`apiUrl` 统一生成 API 地址。
- `request.ts`：`ApiError`、`errorFrom`、`jsonBody`、`requestJson` 负责 JSON 编码、响应解析和错误分类。
- `index.ts`：对上层公开 transport API。

业务 API 只能通过这里发普通 HTTP 请求；SSE 的流读取仍由 `../conversations/chat.ts` 组织，但复用这里的 URL 与错误类型。
