import { useEffect, useState } from "react";
import { Alert, Button, Form, Input } from "antd";
import { useLocation, useNavigate } from "react-router-dom";
import { ApiError, register as registerRequest, requestRegisterCode } from "../../api";
import { useAuth } from "../../auth/AuthProvider";
import AuthLayout, { AuthTransitionLink } from "./AuthLayout";

interface RegisterValues {
  email: string;
  code: string;
  password: string;
  confirm: string;
}

export default function RegisterPage() {
  const { setUser } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [form] = Form.useForm<RegisterValues>();
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [codeBusy, setCodeBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const initial = new URLSearchParams(location.search).get("email") ?? "";
    setEmail(initial);
    form.setFieldsValue({ email: initial, code: undefined, password: undefined, confirm: undefined });
    setSent(false);
    setCooldown(0);
  }, [form, location.search]);

  useEffect(() => {
    if (!cooldown) return;
    const timer = window.setInterval(() => setCooldown((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [cooldown]);

  async function sendCode() {
    if (codeBusy || cooldown > 0) return;
    setError(null);
    try {
      await form.validateFields(["email"]);
    } catch {
      return;
    }
    setCodeBusy(true);
    try {
      await requestRegisterCode(String(form.getFieldValue("email") ?? ""));
      setSent(true);
      setCooldown(60);
      form.setFieldsValue({ code: undefined, password: undefined, confirm: undefined });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "验证码发送失败，请稍后再试。");
    } finally {
      setCodeBusy(false);
    }
  }

  async function submit(values: RegisterValues) {
    setError(null);
    setBusy(true);
    try {
      const user = await registerRequest(values.email, values.code, values.password);
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
      <Form<RegisterValues>
        form={form}
        className="auth-form"
        layout="vertical"
        requiredMark={false}
        onValuesChange={(changed, values) => {
          if (!Object.prototype.hasOwnProperty.call(changed, "email")) return;
          const nextEmail = String(values.email ?? "");
          setEmail(nextEmail);
          setSent(false);
          setCooldown(0);
          form.setFieldsValue({ code: undefined, password: undefined, confirm: undefined });
        }}
        onFinish={(values) => void submit(values)}
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
        <div className="code-row" style={{ marginBottom: 16 }}>
          <Form.Item
            className="code-field"
            style={{ marginBottom: 0 }}
            label="邮箱验证码"
            name="code"
            rules={[{ required: true, len: 6, message: "请输入 6 位验证码。" }]}
          >
            <Input.OTP
              length={6}
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              formatter={(value) => value.replace(/\D/g, "").slice(0, 6)}
              disabled={!sent}
              aria-label="邮箱验证码"
            />
          </Form.Item>
          <Button className="code-button" style={{ height: 48, minHeight: 48 }} type="default" onClick={() => void sendCode()} loading={codeBusy} disabled={!email || cooldown > 0 || codeBusy}>
            {cooldown ? `${cooldown}s 后重发` : sent ? "重新发送" : "发送验证码"}
          </Button>
        </div>
        <Form.Item
          label="密码"
          name="password"
          rules={[{ required: true, min: 12, max: 128, message: "密码长度必须为 12–128 个字符。" }]}
        >
          <Input.Password autoComplete="new-password" placeholder="12–128 个字符" minLength={12} maxLength={128} required disabled={!sent} />
        </Form.Item>
        <Form.Item
          label="确认密码"
          name="confirm"
          dependencies={["password"]}
          rules={[
            { required: true, min: 12, max: 128, message: "请再次输入 12–128 个字符的密码。" },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue("password") === value) return Promise.resolve();
                return Promise.reject(new Error("两次输入的密码不一致。"));
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" placeholder="再次输入密码" minLength={12} maxLength={128} required disabled={!sent} />
        </Form.Item>
        {error ? <Alert className="form-error" title={error} type="error" showIcon /> : null}
        <Button className="primary-cta form-submit" type="primary" htmlType="submit" loading={busy} disabled={!sent} block>
          创建账号
        </Button>
      </Form>
      <div className="form-links"><span>已有账号？ <AuthTransitionLink target="login">立即登录</AuthTransitionLink></span></div>
    </AuthLayout>
  );
}
