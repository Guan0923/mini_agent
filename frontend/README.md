# Mini-Agent Web

一个仿照通义千问（qianwen.com）的网页界面，包含两个部分：

- **对话**：和 mini-agent 聊天，实时看它的工具调用（读文件、跑命令、Web 搜索等）和流式回答
- **Benchmark 成绩单**：在网页上跑 benchmark 任务，看每道题的分数、耗时、token 和判卷明细

后端是 FastAPI（`backend/src/api/`），通过 SSE 把 agent 的运行事件实时推给前端。未登录用户看到 Three.js 粒子海洋首页；登录后进入隔离的聊天与 Benchmark 空间。

## 本地启动

前端只访问本机 backend。账户、验证码和 PostgreSQL 由独立 cloud 服务负责；backend 不需要 `DATABASE_URL`，没有 cloud 时仍可使用游客模式和本地 Agent。

```powershell
docker compose up -d postgres cloud
$env:CLOUD_URL = "http://127.0.0.1:8100" # 仅本机开发允许 HTTP
uv sync
```

然后启动两个进程：

**1. 后端**（FastAPI，端口 8000）

```bash
uv run python -m backend.api
```

**2. 前端**（Vite，端口 5173）

```bash
cd frontend
npm install     # 首次需要
npm run dev
```

然后浏览器打开 http://localhost:5173 。Vite 会把 `/api` 和 `/benchmark` 代理到本机 backend 的 8000 端口。浏览器不会直连 cloud；模型 Provider 配置和 API Key 由登录用户在 Web 设置页填写；服务端不会从独立 TUI 的 `~/.mini_agent/config.toml` 导入模型密钥。

注册及密码重置由 cloud 的 SMTP 配置发送；本地 backend 不读取 SMTP 或 PostgreSQL 环境变量。

## 生产本地 backend 配置

生产模式由本机 backend 提供 `frontend/dist`，浏览器和 backend 使用同一 loopback origin。backend 通过部署环境变量配置精确来源并启用 Secure Cookie：

```toml
[web]
public_url = "https://app.example.com"
allowed_origins = ["https://app.example.com"]
cookie_secure = true
```

浏览器请求（包括 SSE 和 Benchmark）始终携带凭据。会话 Cookie 是本机 backend 的 host-only Cookie，并使用 `HttpOnly`、`SameSite=Lax` 和 `Secure`；不要配置宽泛的 Cookie Domain。`GET /api/ready` 会检查认证和设置数据库，正常返回 `200`，数据库故障返回 `503`。

## 存储迁移检查

本地用户目录、正式账户、云端密钥封装和快照表会原地复用。升级前后可运行 `python scripts/reset_storage_architecture.py` 生成只读兼容性报告；脚本不会删除任何本地或云端数据，cloud 启动时只执行增量 schema migration。

## 配置说明

- cloud 从环境读取 `DATABASE_URL`、`MINI_AGENT_SECRET_KEY` 和 SMTP 配置；backend 从 `CLOUD_URL`（或 `MINI_AGENT_CLOUD_URL`）读取 cloud HTTPS 地址。
- PostgreSQL 保存账户、密码哈希、验证码、登录/设备会话、限流、用户数据密钥封装和加密快照；backend 的 `~/.mini_agent/client.db` 只保存本地会话哈希和身份缓存，每个用户的 `~/.mini_agent/<user_id>/user.db` 保存提供商集合、加密 API Key、加密 cloud token 与同步事务状态。
- 每个用户的对话工作区、会话库和上传文件都在 `~/.mini_agent/<user_id>/runtime/<session_id>/` 下隔离保存，benchmark 沙箱位于用户树之外。
- 聊天历史、工具结果和 RuntimeState 保存在用户电脑上的本地 SQLite；cloud 永远只接收本地打包后的加密快照。

## 结构

```
backend/src/api/                FastAPI 后端（认证 / app / chat / benchmark）
frontend/                React + Vite + TypeScript 前端
  src/components/OceanScene.tsx  GPU 粒子海洋与鼠标波浪
  src/pages/HomePage.tsx         未登录首页
  src/pages/auth/                登录、注册、重置密码、CLI 授权
  src/pages/ChatPage.tsx        对话页（千问风格）
  src/pages/BenchmarkPage.tsx   Benchmark 成绩单页
webapp-data/             运行时数据（已 gitignore）
```
