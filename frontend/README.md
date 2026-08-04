# Mini-Agent Web

一个仿照通义千问（qianwen.com）的网页界面，包含两个部分：

- **对话**：和 mini-agent 聊天，实时看它的工具调用（读文件、跑命令、Web 搜索等）和流式回答
- **Benchmark 成绩单**：在网页上跑 benchmark 任务，看每道题的分数、耗时、token 和判卷明细

后端是 FastAPI（`src/backend/api/`），通过 SSE 把 agent 的运行事件实时推给前端。

## 启动

需要两个进程：

**1. 后端**（FastAPI，端口 8000）

```bash
# 指定模型配置（任选其一）
MINI_AGENT_CONFIG=benchmarks/output/llmtest/config.toml uv run python -m backend.api
# 或者用 ~/mini_agent/config.toml（需先在里面配好 [model] 的 api_key/base_url/model）
uv run python -m backend.api
```

**2. 前端**（Vite，端口 5173）

```bash
cd frontend
npm install     # 首次需要
npm run dev
```

然后浏览器打开 http://localhost:5173 。Vite 会把 `/api` 代理到后端的 8000 端口。

## 配置说明

- 后端从 `MINI_AGENT_CONFIG` 环境变量读取模型配置，缺省用 `~/mini_agent/config.toml`。
- 模型配置需包含非空的 `[model]` 的 `api_key` / `base_url` / `model`（任意 OpenAI 兼容接口）。
  没配好时，对话和 benchmark 会返回清晰的"模型未配置"提示。
- 对话的工作区在 `webapp-data/chat-workspace/`（agent 读写文件都发生在那里，方便查看它做了什么）。
- benchmark 的运行数据在 `webapp-data/sandbox/`。

## 结构

```
src/backend/api/         FastAPI 后端（app / chat / benchmark）
frontend/                React + Vite + TypeScript 前端
  src/pages/ChatPage.tsx        对话页（千问风格）
  src/pages/BenchmarkPage.tsx   Benchmark 成绩单页
webapp-data/             运行时数据（已 gitignore）
```
