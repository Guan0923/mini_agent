/** Resolve API paths on the local backend's same-origin contract. */
export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) {
    throw new Error("Frontend API requests must use the local backend origin.");
  }
  return path.startsWith("/") ? path : `/${path}`;
}
