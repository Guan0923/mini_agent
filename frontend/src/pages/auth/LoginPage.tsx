import { FormEvent, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../../api";
import { useAuth } from "../../auth/AuthProvider";
import AuthLayout from "./AuthLayout";

function safeNext(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/app";
  try {
    const target = new URL(value, window.location.origin);
    if (target.origin !== window.location.origin) return "/app";
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return "/app";
  }
}

export default function LoginPage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const next = new URLSearchParams(location.search).get("next");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await signIn(email, password);
      navigate(safeNext(next), { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "登录失败，请稍后再试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout title="欢迎回来" subtitle="登录后继续你的智能体工作流。">
      <form className="auth-form" onSubmit={submit}>
        <label>邮箱<input type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" required /></label>
        <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="输入密码" required /></label>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <button className="primary-cta form-submit" type="submit" disabled={busy}>{busy ? "登录中…" : "登录"}</button>
      </form>
      <div className="form-links"><Link to={`/forgot-password${email ? `?email=${encodeURIComponent(email)}` : ""}`}>忘记密码？</Link><span>还没有账号？ <Link to="/register">立即注册</Link></span></div>
    </AuthLayout>
  );
}
