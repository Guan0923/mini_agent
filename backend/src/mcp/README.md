# MCP 客户端

该包仅提供审批型外部 MCP 客户端。SDK 2.x 自动选择 2026-07-28 或旧版协议。

- `config.py`：stdio / Streamable HTTP 配置与凭据引用校验。
- `settings.py`：TOML 保存及 OS 凭据库的保留、替换、删除。
- `controlled_stdio.py`：保留受控子进程的启动、环境和关闭方式。
- `client/`：连接、工具适配、资源和提示词、运行内订阅。

Agent 使用 `list_mcp_resources`、`list_mcp_resource_templates`、`read_mcp_resource`、`subscribe_mcp_resource`、`unsubscribe_mcp_resource`、`get_mcp_resource_updates`、`list_mcp_prompts`、`get_mcp_prompt`，均经过现有审批。列表保留 cursor；工具发现仍获取全部页。

资源 URI 只交给选定服务处理。文本输出最多 20,000 字符，非文本内容只返回元数据；提示词作为不可信工具结果返回。订阅不跨运行，不触发自动读取或新对话，断线和溢出会明确提示需要重新检查。

本地测试：先以 `uv venv .tmp-mcp-v1` 和 `uv pip install --python .tmp-mcp-v1/Scripts/python.exe mcp==1.28.1 uvicorn` 安装独立旧版服务环境，或通过 `MINI_AGENT_MCP_V1_PYTHON` 指定该环境。执行 `uv run python -m pytest tests/test_mcp_capabilities.py tests/test_mcp_http_settings.py` 验证真实 stdio / HTTP 新旧协议矩阵。

浏览器测试：在 frontend 执行 `npx playwright test --config playwright.mcp.config.ts`，使用真实 backend、Vite、本地 MCP 和假模型。测试使用独立端口与 Redis 前缀，不调用付费模型。
