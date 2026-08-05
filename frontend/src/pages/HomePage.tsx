import { ArrowRightOutlined } from "@ant-design/icons";
import { Link } from "react-router-dom";

export default function HomePage() {
  return (
    <main className="landing-page">
      <div className="landing-content">
        <header className="landing-header">
          <Link className="brand-mark" to="/">MINI<span>·</span>AGENT</Link>
          <nav className="landing-nav" aria-label="账户导航">
            <Link className="text-link" to="/login">登录</Link>
            <Link className="outline-cta" to="/register">注册</Link>
          </nav>
        </header>
        <section className="hero-copy">
          <p className="eyebrow">A calmer way to build with agents</p>
          <h1><span className="hero-title-lead">让想法像潮汐一样，</span><span>自然地流动。</span></h1>
          <p className="hero-description">Mini-Agent 将规划、工具和执行编织在一起。把复杂任务交给智能体，把注意力留给真正重要的创造。</p>
          <div className="hero-actions">
            <Link className="primary-cta" to="/register">开始探索 <ArrowRightOutlined aria-hidden="true" /></Link>
            <Link className="secondary-cta" to="/login">已有账号，登录</Link>
          </div>
        </section>
        <footer className="landing-footer">
          <span>规划 · 工具 · 技能 · 可观察执行</span>
        </footer>
      </div>
    </main>
  );
}
