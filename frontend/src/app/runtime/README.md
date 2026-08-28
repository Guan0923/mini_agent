# Runtime 前端投影

该包把 backend 的 RuntimeRoot/Turn 节点与增量帧转换成稳定 UI 状态。

- `runtimeNodeNormalization.ts`：`normalizeRuntimeNode`、`normalizeRuntimeNodeModel`、`isRuntimeTurnNode`/`isRuntimeRootNode` 处理协议边界。
- `runtimeNodeReducer.ts`：`runtimeNodeAccumulator`、`applyRuntimeNodeFrame`、`leafNodes` 合并 snapshot/delta。
- `runtimeNodeProjection.ts`：提取单节点的展示字段。
- `runtimeDetailProjection.ts`：`projectTurnPath`、`projectRuntimeNode`、rewind 剪枝与消息重建。

公开消费者是 `runController`、`AgentApp`、Chat 页面和 Session API。这里仅依赖共享类型，不发请求、不渲染组件。
