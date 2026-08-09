# Mini-Agent Web

一个仿照通义千问（qianwen.com）的网页界面，包含两个部分：

- **对话**：和 mini-agent 聊天，实时看它的工具调用（读文件、跑命令、Web 搜索等）和流式回答
- **Benchmark 成绩单**：在网页上跑 benchmark 任务，看每道题的分数、耗时、token 和判卷明细

后端是 FastAPI（`backend/src/backend/api/`），通过 SSE 把 agent 的运行事件实时推给前端。未登录用户看到 Three.js 粒子海洋首页；登录后进入隔离的聊天与 Benchmark 空间。

## 本地启动

Web 认证和用户设置以 PostgreSQL 为唯一数据源。后端缺少数据库连接、数据库不可用，或没有服务端加密密钥时会直接启动失败，不会创建或回退到 `auth.sqlite3`。

```powershell
docker compose up -d postgres
$env:DATABASE_URL = "postgresql://mini_agent:mini_agent@127.0.0.1:5432/mini_agent"
# 使用密码管理器生成并保存至少 32 个 UTF-8 字节的随机值；不要提交到仓库。
$env:MINI_AGENT_SECRET_KEY = "replace-with-at-least-32-random-bytes"
uv sync --extra web
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

然后浏览器打开 http://localhost:5173 。本地未设置 `VITE_API_BASE_URL` 时，Vite 会把 `/api` 和 `/benchmark` 代理到后端的 8000 端口。模型 Provider 配置和 API Key 由登录用户在 Web 设置页填写；服务端不会导入本机 `~/mini_agent/config.toml` 中的模型密钥。

注册及密码重置需要在 `~/mini_agent/config.toml` 的 `[email]` 中配置 SMTP；验证码不会写入日志。

## 生产跨子域配置

前端和 API 应使用同一主域下的不同 HTTPS 子域，例如 `app.example.com` 与 `api.example.com`。前端构建环境设置：

```text
VITE_API_BASE_URL=https://api.example.com
```

后端的 `~/mini_agent/config.toml` 使用精确来源并启用 Secure Cookie：

```toml
[web]
public_url = "https://app.example.com"
allowed_origins = ["https://app.example.com"]
cookie_secure = true
```

浏览器请求（包括 SSE 和 Benchmark）始终携带凭据。会话 Cookie 是 API 子域的 host-only Cookie，并使用 `HttpOnly`、`SameSite=Lax` 和 `Secure`；不要配置宽泛的 Cookie Domain。`GET /api/ready` 会检查认证和设置数据库，正常返回 `200`，数据库故障返回 `503`。

## 从旧版 SQLite 迁移

迁移只通过显式命令执行，目标连接始终从 `DATABASE_URL` 读取：

```powershell
uv run --extra web mini-agent-migrate-web-auth check --source C:\path\to\auth.sqlite3
uv run --extra web mini-agent-migrate-web-auth apply --source C:\path\to\auth.sqlite3
```

先停止旧 Web 后端并确保 SQLite WAL 已清空。迁移保留用户 ID、密码哈希、资料及非敏感 Agent/Provider 设置，但不迁移浏览器会话、设备授权、验证码、限流数据或模型 API Key。源文件保持只读；相同 SHA-256 的源重复执行为安全无操作。迁移后用户需要重新登录并重新填写模型 API Key。

## 配置说明

- 后端从环境读取 `DATABASE_URL` 和 `MINI_AGENT_SECRET_KEY`，从 `~/mini_agent/config.toml` 读取 Web 与 SMTP 配置。
- PostgreSQL 保存账户、密码哈希、验证码、登录/设备会话、限流、资料及 Agent/Provider 设置；登录和设备令牌只保存 SHA-256 哈希，模型 API Key 使用带版本号的 AES-GCM 密文。
- 每个用户的对话工作区、会话库和 benchmark 沙箱都在 `webapp-data/users/<user_id>/` 下隔离保存。
- 聊天历史、工具结果和 RuntimeState 仍是服务器本地 SQLite；多实例聊天暂时需要单实例、粘性路由或共享本地卷。

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
