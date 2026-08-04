import { Link } from "react-router-dom";
import OceanScene from "../components/OceanScene";

export default function HomePage() {
  return (
    <main className="landing-page">
      <OceanScene />
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
          <h1>让想法像潮汐一样，<br /><span>自然地流动。</span></h1>
          <p className="hero-description">Mini-Agent 将规划、工具和执行编织在一起。把复杂任务交给智能体，把注意力留给真正重要的创造。</p>
          <div className="hero-actions">
            <Link className="primary-cta" to="/register">开始探索 <span aria-hidden="true">→</span></Link>
            <Link className="secondary-cta" to="/login">已有账号，登录</Link>
          </div>
        </section>
        <footer className="landing-footer">
          <span>规划 · 工具 · 技能 · 可观察执行</span>
          <span className="footer-hint">左右移动鼠标，感受波浪</span>
        </footer>
      </div>
    </main>
  );
}
