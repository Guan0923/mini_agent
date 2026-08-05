import { useState } from "react";
import { Alert, Button, Form, Input } from "antd";
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
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(values: { email: string; password: string }) {
    setError(null);
    setBusy(true);
    try {
      await signIn(values.email, values.password);
      navigate(safeNext(next), { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "登录失败，请稍后再试。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout title="欢迎回来" subtitle="登录后继续你的智能体工作流。">
      <Form<{ email: string; password: string }>
        className="auth-form"
        layout="vertical"
        onValuesChange={(changed) => {
          if (Object.prototype.hasOwnProperty.call(changed, "email")) setEmail(String(changed.email ?? ""));
        }}
        onFinish={(values) => void submit(values)}
        requiredMark={false}
      >
        <Form.Item
          label="邮箱"
          name="email"
          rules={[
            { required: true, message: "请输入邮箱。" },
            { type: "email", message: "请输入有效的邮箱地址。" },
          ]}
        >
          <Input type="email" autoComplete="email" placeholder="you@example.com" required />
        </Form.Item>
        <Form.Item label="密码" name="password" rules={[{ required: true, message: "请输入密码。" }]}>
          <Input.Password autoComplete="current-password" placeholder="输入密码" required />
        </Form.Item>
        {error ? <Alert className="form-error" message={error} type="error" showIcon /> : null}
        <Button className="primary-cta form-submit" type="primary" htmlType="submit" loading={busy} block>
          登录
        </Button>
      </Form>
      <div className="form-links"><Link to={`/forgot-password${email ? `?email=${encodeURIComponent(email)}` : ""}`}>忘记密码？</Link><span>还没有账号？ <Link to="/register">立即注册</Link></span></div>
    </AuthLayout>
  );
}
