# Sandbox Control Plane

该包在每个 tool_call 前执行审批和执行决策。

- `operation.py`：`sandbox_operation` Hook，生成审批并返回 `HookOperationResult`。
- `approvals.py`：`ApprovalStore`、授权 hash 和 grant。
- `authorization.py`：参数/路径授权规则；`decision.py`：`SandboxExecutionDecision`。
- `broker.py`：`WindowsBrokerClient`、Broker process stream。
- `__init__.py`：惰性公开入口，避免与 runtime launcher 循环。

Control 不负责 Handler 的路径/URL 语义校验；Broker 健康失败必须拒绝而非降级。
`web_search` 和 `web_fetch` 仅在 `full_network` 下免审批；其他网络模式逐次审批，
批准后的 Backend Web 请求不消费 `run_command` 的 WFP/Proxy 白名单。
