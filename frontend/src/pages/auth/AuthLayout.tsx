import { Card } from "antd";
import { Link, type LinkProps } from "react-router-dom";

type AuthTarget = "login" | "register" | "forgot-password";

interface AuthTransitionLinkProps extends Omit<LinkProps, "to"> {
  target: AuthTarget;
  search?: string;
}

export function AuthTransitionLink({ target, search = "", onClick, ...props }: AuthTransitionLinkProps) {
  const normalizedSearch = search && !search.startsWith("?") ? `?${search}` : search;
  const to = `/${target}${normalizedSearch}`;

  return <Link {...props} to={to} onClick={onClick} />;
}

export default function AuthLayout({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <main className="auth-page">
      <div className="auth-overlay">
        <Link className="brand-mark auth-brand" to="/">MINI<span>·</span>AGENT</Link>
        <Card className="auth-card auth-card--staggered-reveal" variant="borderless" styles={{ body: { padding: 0 } }}>
          <p className="eyebrow">MINI·AGENT</p>
          <h1>{title}</h1>
          <p className="auth-subtitle">{subtitle}</p>
          <div className="auth-card-content">{children}</div>
        </Card>
        <p className="auth-back auth-back--reveal"><Link to="/">← 返回首页</Link></p>
      </div>
    </main>
  );
}
