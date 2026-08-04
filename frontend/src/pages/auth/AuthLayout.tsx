import { Link } from "react-router-dom";
import OceanScene from "../../components/OceanScene";

export default function AuthLayout({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <main className="auth-page">
      <OceanScene />
      <div className="auth-overlay">
        <Link className="brand-mark auth-brand" to="/">MINI<span>·</span>AGENT</Link>
        <section className="auth-card">
          <p className="eyebrow">MINI·AGENT</p>
          <h1>{title}</h1>
          <p className="auth-subtitle">{subtitle}</p>
          {children}
        </section>
        <p className="auth-back"><Link to="/">← 返回首页</Link></p>
      </div>
    </main>
  );
}
