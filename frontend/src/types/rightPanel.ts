export interface RightPanelState {
  session_id: string;
  width: number;
  collapsed: boolean;
  active_window_id: string | null;
}

export interface RightPanelWindow {
  id: string;
  session_id: string;
  kind: "side_chat" | "terminal";
  title: string;
  position: number;
  created_at: string;
  updated_at: string;
  thread_id: string | null;
  anchor_turn_id: string | null;
  terminal_id: string | null;
  terminal_type: string | null;
  cwd: string | null;
  deleted_at: string | null;
}

export interface RightPanelPayload {
  state: RightPanelState;
  windows: RightPanelWindow[];
  capabilities: {
    terminal_available: boolean;
    terminal_unavailable_reason: string | null;
  };
}
