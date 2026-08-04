import { FormEvent, useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ApiError, register as registerRequest, requestRegisterCode } from "../../api";
import { useAuth } from "../../auth/AuthProvider";
import AuthLayout from "./AuthLayout";

export default function RegisterPage() {
  const { setUser } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [sent, setSent] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const initial = new URLSearchParams(location.search).get("email");
    if (initial) setEmail(initial);
  }, [location.search]);

  useEffect(() => {
    if (!cooldown) return;
    const timer = window.setInterval(() => setCooldown((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [cooldown]);

  async function sendCode() {
    setError(null);
    try {
      await requestRegisterCode(email);
      setSent(true);
      setCooldown(60);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "验证码发送失败，请稍后再试。");
    }
  }

  function updateEmail(value: string) {
    setEmail(value);
    setCode("");
    setSent(false);
    setCooldown(0);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (password !== confirm) {
      setError("两次输入的密码不一致。");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const user = await registerRequest(email, code, password);
      setUser(user);
      navigate("/app", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "注册失败，请稍后再试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout title="创建你的账号" subtitle="用邮箱验证身份，开始一段新的工作流。">
      <form className="auth-form" onSubmit={submit}>
        <label>邮箱<input type="email" autoComplete="email" value={email} onChange={(e) => updateEmail(e.target.value)} placeholder="you@example.com" required /></label>
        <div className="code-row"><label>邮箱验证码<input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} placeholder="6 位验证码" required disabled={!sent} /></label><button className="code-button" type="button" onClick={() => void sendCode()} disabled={!email || cooldown > 0}>{cooldown ? `${cooldown}s 后重发` : sent ? "重新发送" : "发送验证码"}</button></div>
        <label>密码<input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="至少 12 个字符" minLength={12} required disabled={!sent} /></label>
        <label>确认密码<input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder="再次输入密码" minLength={12} required disabled={!sent} /></label>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <button className="primary-cta form-submit" type="submit" disabled={busy || !sent}>{busy ? "创建中…" : "创建账号"}</button>
      </form>
      <div className="form-links"><span>已有账号？ <Link to="/login">立即登录</Link></span></div>
    </AuthLayout>
  );
}
