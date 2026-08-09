/** Resolve API paths for same-origin development or a dedicated API subdomain. */
export function apiUrl(path: string): string {
  const configured = String(import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/+$/, "");
  if (!configured || /^https?:\/\//i.test(path)) return path;
  return `${configured}${path.startsWith("/") ? path : `/${path}`}`;
}
