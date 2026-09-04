import { COMMANDS, type CommandDefinition } from "./index";

export interface CommandTrigger {
  query: string;
  start: number;
  end: number;
}

export function commandTrigger(input: string, caret: number): CommandTrigger | null {
  const head = input.slice(0, caret);
  const match = /(?:^|\s)(\/[^\s/]*)$/.exec(head);
  if (!match) return null;
  const start = match.index + match[0].length - match[1].length;
  return { query: match[1], start, end: caret };
}

export function commandSuggestions(query: string): CommandDefinition[] {
  if (!/^\/[^\s]*$/.test(query)) return [];
  const prefix = query.toLowerCase();
  return COMMANDS.filter((command) => command.name.startsWith(prefix));
}

export function nextCommandIndex(current: number, direction: -1 | 1, count: number): number {
  if (count <= 0) return 0;
  return (current + direction + count) % count;
}

export function completionText(command: CommandDefinition): string {
  return command.name;
}

export type CommandKeyAction =
  | { type: "complete" }
  | { type: "execute" }
  | { type: "dismiss" }
  | { type: "move"; direction: -1 | 1 }
  | { type: "send" }
  | { type: "none" };

export function commandKeyAction({
  key,
  shiftKey,
  isComposing,
  menuVisible,
}: {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
  menuVisible: boolean;
}): CommandKeyAction {
  if (isComposing) return { type: "none" };
  if (menuVisible && (key === "ArrowDown" || key === "ArrowUp")) {
    return { type: "move", direction: key === "ArrowDown" ? 1 : -1 };
  }
  if (menuVisible && key === "Escape") return { type: "dismiss" };
  if (menuVisible && key === "Enter" && !shiftKey) return { type: "execute" };
  if (menuVisible && key === "Tab" && !shiftKey) return { type: "complete" };
  if (key === "Enter" && !shiftKey) return { type: "send" };
  return { type: "none" };
}
