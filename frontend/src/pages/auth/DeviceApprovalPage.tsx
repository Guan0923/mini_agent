import { useEffect, useState } from "react";
import { Alert, Button, Descriptions, Result, Spin } from "antd";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ApiError, approveDevice, deviceInfo } from "../../api";
import { useAuth } from "../../auth/AuthProvider";
import AuthLayout from "./AuthLayout";

type DeviceStatus = "loading" | "ready" | "done" | "error";

export default function DeviceApprovalPage() {
  const { user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const grant = new URLSearchParams(location.search).get("grant") ?? "";
  const [server, setServer] = useState<string | null>(null);
  const [createdAt, setCreatedAt] = useState<number | null>(null);
  const [status, setStatus] = useState<DeviceStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
    if (busy) return;
    setBusy(true);
    try {
      await approveDevice(grant, approved);
      if (!approved) setError("已拒绝此次访问。");
      setStatus("done");
      if (!approved) window.setTimeout(() => navigate("/"), 1000);
    } catch (err) {
      setStatus("error");
      setError(err instanceof ApiError ? err.message : "授权操作失败。");
    } finally {
      setBusy(false);
    }
  }

  if (!user) {
    const next = `${location.pathname}${location.search}`;
    return (
      <AuthLayout title="授权你的终端" subtitle="请先登录，再批准这次设备访问。">
        <div className="device-message">
          <p>登录后即可返回此页确认授权。</p>
          <Link className="primary-cta form-submit" to={`/login?next=${encodeURIComponent(next)}`}>前往登录</Link>
        </div>
      </AuthLayout>
    );
  }

  const details = server || createdAt !== null ? (
    <Descriptions className="device-details" column={1} bordered size="small">
      {server ? <Descriptions.Item label="请求来自"><strong>{server}</strong></Descriptions.Item> : null}
      {createdAt !== null ? <Descriptions.Item label="请求时间">{new Date(createdAt * 1000).toLocaleString()}</Descriptions.Item> : null}
    </Descriptions>
  ) : null;

  return (
    <AuthLayout title="授权终端访问" subtitle="确认后，Terminal 将可以使用你的 Mini-Agent 账号。">
      <div className="device-message">
        {details}
        {status === "loading" ? <Spin tip="正在读取请求…" /> : null}
        {status === "ready" ? (
          <>
            <p>当前登录账号：<strong>{user.email}</strong></p>
            <div className="device-actions">
              <Button className="primary-cta" type="primary" loading={busy} disabled={busy} onClick={() => void decide(true)}>批准访问</Button>
              <Button className="quiet-button" type="text" danger loading={busy} disabled={busy} onClick={() => void decide(false)}>拒绝</Button>
            </div>
          </>
        ) : null}
        {status === "done" ? (
          error ? (
            <>
              <Result status="warning" title="设备访问已拒绝" />
              <Alert message={error} type="warning" showIcon />
            </>
          ) : <Result status="success" title="授权成功，可以返回 Terminal。" />
        ) : null}
        {status === "error" ? (
          <>
            <Result status="error" title="无法完成设备授权" />
            {error ? <Alert message={error} type="error" showIcon /> : null}
          </>
        ) : null}
      </div>
    </AuthLayout>
  );
}
