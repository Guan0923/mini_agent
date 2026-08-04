# Mini-Agent Web

一个仿照通义千问（qianwen.com）的网页界面，包含两个部分：

- **对话**：和 mini-agent 聊天，实时看它的工具调用（读文件、跑命令、Web 搜索等）和流式回答
- **Benchmark 成绩单**：在网页上跑 benchmark 任务，看每道题的分数、耗时、token 和判卷明细

后端是 FastAPI（`backend/src/backend/api/`），通过 SSE 把 agent 的运行事件实时推给前端。未登录用户看到 Three.js 粒子海洋首页；登录后进入隔离的聊天与 Benchmark 空间。

## 启动

需要两个进程：

**1. 后端**（FastAPI，端口 8000）

```bash
# 使用 ~/mini_agent/config.toml（需先配好 [model] 的 api_key/base_url/model）
uv run python -m backend.api
```

**2. 前端**（Vite，端口 5173）

```bash
cd frontend
npm install     # 首次需要
npm run dev
```

然后浏览器打开 http://localhost:5173 。Vite 会把 `/api` 和 `/benchmark` 代理到后端的 8000 端口。

注册需要在 `~/mini_agent/config.toml` 的 `[email]` 中配置 SMTP；验证码不会写入日志。生产 HTTPS 部署时将 `[web].cookie_secure` 设为 `true`，并同步设置 `public_url` 与 `allowed_origins`。

## 配置说明

- 后端从 `~/mini_agent/config.toml` 读取模型、Web 和 SMTP 配置。
- 模型配置需包含非空的 `[model]` 的 `api_key` / `base_url` / `model`（任意 OpenAI 兼容接口）。
  没配好时，对话和 benchmark 会返回清晰的"模型未配置"提示。
- 每个用户的对话工作区、会话库和 benchmark 沙箱都在 `webapp-data/users/<user_id>/` 下隔离保存。

## 结构

```
backend/src/backend/api/         FastAPI 后端（认证 / app / chat / benchmark）
frontend/                React + Vite + TypeScript 前端
  src/components/OceanScene.tsx  GPU 粒子海洋与鼠标波浪
  src/pages/HomePage.tsx         未登录首页
  src/pages/auth/                登录、注册、重置密码、CLI 授权
  src/pages/ChatPage.tsx        对话页（千问风格）
  src/pages/BenchmarkPage.tsx   Benchmark 成绩单页
webapp-data/             运行时数据（已 gitignore）
```
