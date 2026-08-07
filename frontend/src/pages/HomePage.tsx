import { ArrowRightOutlined } from "@ant-design/icons";
import { Link, useNavigate, useOutletContext } from "react-router-dom";
import type { AuthTarget, PublicOutletContext } from "./PublicLayout";

export default function HomePage() {
  const navigate = useNavigate();
  const context = useOutletContext<PublicOutletContext>();
  const openAuth = context?.openAuth ?? ((target: AuthTarget) => navigate(`/${target}`));

  function authLink(target: AuthTarget) {
    return (event: React.MouseEvent<HTMLAnchorElement>) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      openAuth(target);
    };
  }

  return (
    <main className="landing-page">
      <div className="landing-content">
        <header className="landing-header">
          <Link className="brand-mark" to="/">MINI<span>·</span>AGENT</Link>
          <nav className="landing-nav" aria-label="账户导航">
            <Link className="text-link" to="/login" onClick={authLink("login")}>登录</Link>
            <Link className="outline-cta" to="/register" onClick={authLink("register")}>注册</Link>
          </nav>
        </header>
        <section className="hero-copy">
          <p className="eyebrow">A calmer way to build with agents</p>
          <h1><span className="hero-title-lead">让想法像潮汐一样，</span><span>自然地流动。</span></h1>
          <p className="hero-description">Mini-Agent 将规划、工具和执行编织在一起。把复杂任务交给智能体，把注意力留给真正重要的创造。</p>
          <div className="hero-actions">
            <Link className="primary-cta" to="/register" onClick={authLink("register")}>开始探索 <ArrowRightOutlined aria-hidden="true" /></Link>
            <Link className="secondary-cta" to="/register" onClick={authLink("register")}>没有账号，注册</Link>
          </div>
        </section>
        <footer className="landing-footer">
          <span>规划 · 工具 · 技能 · 可观察执行</span>
        </footer>
      </div>
    </main>
  );
}
