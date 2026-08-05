import { Card } from "antd";
import { Link } from "react-router-dom";

export default function AuthLayout({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <main className="auth-page">
      <div className="auth-overlay">
        <Link className="brand-mark auth-brand" to="/">MINI<span>·</span>AGENT</Link>
        <Card className="auth-card" variant="borderless" styles={{ body: { padding: 0 } }}>
          <p className="eyebrow">MINI·AGENT</p>
          <h1>{title}</h1>
          <p className="auth-subtitle">{subtitle}</p>
          {children}
        </Card>
        <p className="auth-back"><Link to="/">← 返回首页</Link></p>
      </div>
    </main>
  );
}
