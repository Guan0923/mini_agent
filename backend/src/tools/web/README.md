# Safe Web Tools

该包实现受网络策略约束的搜索和抓取。

- `search.py`：`DdgrWebSearch`；`fetch.py`：`SafeWebFetcher`、redirect/peer 校验。
- `protocols.py`：resolver/HTTP 端口；`html.py`、`text.py`：内容抽取和空白归一化。
- `__init__.py`：公开 Web Handler。

仅允许 HTTP(S)、策略允许的 host/port 与公共 DNS；每次 redirect 和连接 peer 都需重新校验，响应大小有上限。
