# Provider 层

该包统一模型配置、canonical request/response 与 HTTP transport。

- `__init__.py`：汇总公开 Provider client、adapter、配置、错误与规范化转换接口。
- `protocols.py`、`adapters.py`：Provider/Adapter 协议。
- `canonical.py`：跨 Provider 的规范化消息、tool call 与流事件。
- `client.py`：Provider client 门面；`transport.py`：`JsonHttpTransport`。
- `config.py`、`token_usage.py`、`errors.py`：配置、token 统计和错误分类。
- `chat_completions/`：Chat Completions 兼容协议实现。

Provider 不得导入 storage；API key 只通过已解析配置进入 transport，日志不得回显 secret/header。

Provider 异常类型可以保存重试、HTTP、request id 与流状态元数据，但异常文本必须保持底层 transport/JSON 根因原消息并在外部投影时脱敏。
