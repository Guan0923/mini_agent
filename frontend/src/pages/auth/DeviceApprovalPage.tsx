import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ApiError, approveDevice, deviceInfo } from "../../api";
import { useAuth } from "../../auth/AuthProvider";
import AuthLayout from "./AuthLayout";

export default function DeviceApprovalPage() {
  const { user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const grant = new URLSearchParams(location.search).get("grant") ?? "";
  const [server, setServer] = useState<string | null>(null);
  const [createdAt, setCreatedAt] = useState<number | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "done" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!grant) {
      setStatus("error");
      setError("授权链接无效。");
      return;
    }
    deviceInfo(grant)
      .then((info) => {
        setServer(info.server_url);
        setCreatedAt(info.created_at);
        setStatus(info.status === "pending" ? "ready" : "error");
        if (info.status !== "pending") setError("授权请求已处理或已过期。");
      })
      .catch((err) => {
        setStatus("error");
        setError(err instanceof ApiError ? err.message : "无法读取授权请求。");
      });
  }, [grant]);

  async function decide(approved: boolean) {
    try {
      await approveDevice(grant, approved);
      if (!approved) setError("已拒绝此次访问。");
      setStatus("done");
      if (!approved) window.setTimeout(() => navigate("/"), 1000);
    } catch (err) {
      setStatus("error");
      setError(err instanceof ApiError ? err.message : "授权操作失败。");
    }
  }

  if (!user) {
    const next = `${location.pathname}${location.search}`;
    return (
      <AuthLayout title="授权你的终端" subtitle="请先登录，再批准这次设备访问。">
        <div className="device-message"><p>登录后即可返回此页确认授权。</p><Link className="primary-cta form-submit" to={`/login?next=${encodeURIComponent(next)}`}>前往登录</Link></div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title="授权终端访问" subtitle="确认后，Terminal 将可以使用你的 Mini-Agent 账号。">
      <div className="device-message">
        {server ? <p className="device-server">请求来自 <strong>{server}</strong></p> : null}
        {createdAt !== null ? <p className="device-time">请求时间：{new Date(createdAt * 1000).toLocaleString()}</p> : null}
        {status === "loading" ? <p>正在读取请求…</p> : null}
        {status === "ready" ? <><p>当前登录账号：<strong>{user.email}</strong></p><div className="device-actions"><button className="primary-cta" onClick={() => void decide(true)}>批准访问</button><button className="quiet-button" onClick={() => void decide(false)}>拒绝</button></div></> : null}
        {status === "done" ? <p className={error ? "form-error" : "success-text"}>{error ?? "授权成功，可以返回 Terminal。"}</p> : null}
        {error && status !== "done" ? <p className="form-error" role="alert">{error}</p> : null}
      </div>
    </AuthLayout>
  );
}
