# Chat Completions Provider

该包把 canonical Provider 请求适配到 Chat Completions 兼容服务。

- `adapter.py`：Provider adapter 门面。
- `requests.py`、`messages.py`、`models.py`：请求、消息/tool schema 和模型字段转换。
- `streaming.py`、`responses.py`：流式与非流式响应解析。
- `common.py`：该协议内部共享的严格转换。
- `__init__.py`：公开 adapter。

协议层依赖 providers canonical/transport；不得读取 storage 或记录 API key。
