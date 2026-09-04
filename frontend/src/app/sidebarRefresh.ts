export const SIDEBAR_REFRESH_INTERVAL_MS = 5_000;

export function subscribeVisibleSidebarRefresh(
  refresh: () => Promise<void>,
  documentTarget: Document = document,
  windowTarget: Window = window,
): () => void {
  let timer: number | undefined;
  const refreshSilently = () => {
    void refresh().catch(() => undefined);
  };
  const stopTimer = () => {
    if (timer === undefined) return;
    windowTarget.clearInterval(timer);
    timer = undefined;
  };
  const startTimer = () => {
    if (documentTarget.visibilityState !== "visible" || timer !== undefined) return;
    timer = windowTarget.setInterval(refreshSilently, SIDEBAR_REFRESH_INTERVAL_MS);
  };
  const onVisibilityChange = () => {
    stopTimer();
    if (documentTarget.visibilityState !== "visible") return;
    refreshSilently();
    startTimer();
  };

  startTimer();
  documentTarget.addEventListener("visibilitychange", onVisibilityChange);
  return () => {
    stopTimer();
    documentTarget.removeEventListener("visibilitychange", onVisibilityChange);
  };
}
