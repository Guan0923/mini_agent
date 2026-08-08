export default function ShimmerText({ children, active }: { children: string; active: boolean }) {
  return <span className={active ? "shimmer-text is-active" : "shimmer-text"}>{children}</span>;
}
