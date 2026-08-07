import { Card } from "antd";
import { useLayoutEffect, type MouseEvent as ReactMouseEvent } from "react";
import { Link, useLocation, useOutletContext, type LinkProps } from "react-router-dom";
import type { AuthRoute, AuthTarget, PublicOutletContext } from "../PublicLayout";

function isPlainLeftClick(event: ReactMouseEvent<HTMLAnchorElement>): boolean {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

function routeForPath(pathname: string): AuthRoute {
  if (pathname === "/register") return "register";
  if (pathname === "/forgot-password") return "forgot-password";
  if (pathname === "/device/approve") return "device";
  return "login";
}

interface AuthTransitionLinkProps extends Omit<LinkProps, "to"> {
  target: AuthTarget;
  search?: string;
}

export function AuthTransitionLink({ target, search = "", onClick, ...props }: AuthTransitionLinkProps) {
  const context = useOutletContext<PublicOutletContext | null>();
  const normalizedSearch = search && !search.startsWith("?") ? `?${search}` : search;
  const to = `/${target}${normalizedSearch}`;

  return (
    <Link
      {...props}
      to={to}
      onClick={(event) => {
        onClick?.(event);
        if (event.defaultPrevented || !context || !isPlainLeftClick(event)) return;
        event.preventDefault();
        context.openAuth(target, { search });
      }}
    />
  );
}

export default function AuthLayout({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  const location = useLocation();
  const context = useOutletContext<PublicOutletContext | null>();
  const route = routeForPath(location.pathname);
  const activeTransition = context?.authEffectsEnabled ? context.transition : null;
  let transitionClass = "";
  if (activeTransition?.phase === "enter" && activeTransition.target === route) transitionClass = " auth-card--emerge";
  if (activeTransition?.phase === "switch-out" && activeTransition.source === route) transitionClass = " auth-card--switch-out";
  if (activeTransition?.phase === "exit" && activeTransition.source === route) transitionClass = " auth-card--sink";

  useLayoutEffect(() => {
    context?.registerAuthSnapshot({ pathname: location.pathname, route, title, subtitle });
  }, [context, location.pathname, route, subtitle, title]);

  const closeHome = (event: ReactMouseEvent<HTMLAnchorElement>) => {
    if (!context || !isPlainLeftClick(event)) return;
    event.preventDefault();
    context.closeAuth();
  };

  return (
    <main className="auth-page">
      <div className="auth-overlay">
        <Link className="brand-mark auth-brand" to="/" onClick={closeHome}>MINI<span>·</span>AGENT</Link>
        <Card className={`auth-card${transitionClass}`} variant="borderless" styles={{ body: { padding: 0 } }}>
          <p className="eyebrow">MINI·AGENT</p>
          <h1>{title}</h1>
          <p className="auth-subtitle">{subtitle}</p>
          {children}
        </Card>
        <p className="auth-back"><Link to="/" onClick={closeHome}>← 返回首页</Link></p>
      </div>
    </main>
  );
}
