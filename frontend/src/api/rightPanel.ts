import type { RightPanelPayload, RightPanelWindow, RuntimeStateNode } from "../types";
import { jsonBody, requestJson } from "./transport/request";

export interface CreatedSideChat {
  window: RightPanelWindow;
  anchor: RuntimeStateNode;
}

export interface CreatedTerminal {
  window: RightPanelWindow;
  terminal: {
    id: string;
    terminal_type: string;
    terminal_label: string;
    cwd: string;
    last_sequence: number;
    exit_code: number | null;
    alive: boolean;
  };
}

const base = (sessionId: string) => `/api/right-panel/${encodeURIComponent(sessionId)}`;

export function getRightPanel(sessionId: string): Promise<RightPanelPayload> {
  return requestJson(base(sessionId));
}

export function updateRightPanel(
  sessionId: string,
  patch: Partial<Pick<RightPanelPayload["state"], "width" | "collapsed" | "active_window_id">>,
): Promise<RightPanelPayload> {
  return requestJson(base(sessionId), { method: "PATCH", ...jsonBody(patch) });
}

export function createSideChat(sessionId: string, sourceTurnId: string): Promise<CreatedSideChat> {
  return requestJson(`${base(sessionId)}/side-chats`, {
    method: "POST",
    ...jsonBody({ source_turn_id: sourceTurnId }),
  });
}

export function createPanelTerminal(sessionId: string, sourceTurnId: string): Promise<CreatedTerminal> {
  return requestJson(`${base(sessionId)}/terminals`, {
    method: "POST",
    ...jsonBody({ source_turn_id: sourceTurnId }),
  });
}

export function renameRightPanelWindow(sessionId: string, windowId: string, title: string): Promise<RightPanelWindow> {
  return requestJson(`${base(sessionId)}/windows/${encodeURIComponent(windowId)}`, {
    method: "PATCH",
    ...jsonBody({ title }),
  });
}

export async function closeRightPanelWindow(sessionId: string, windowId: string): Promise<void> {
  await requestJson(`${base(sessionId)}/windows/${encodeURIComponent(windowId)}`, { method: "DELETE" });
}

export function terminalWebSocketUrl(terminalId: string, afterSequence: number): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const baseUrl = `${scheme}://${window.location.host}`;
  return `${baseUrl}/api/right-panel/terminals/${encodeURIComponent(terminalId)}/ws?after_sequence=${afterSequence}`;
}
