import { COMMANDS, type CommandDefinition } from "./index";

export function commandSuggestions(input: string): CommandDefinition[] {
  if (!/^\/[^\s]*$/.test(input)) return [];
  const prefix = input.toLowerCase();
  return COMMANDS.filter((command) => command.name.startsWith(prefix));
}

export function nextCommandIndex(current: number, direction: -1 | 1, count: number): number {
  if (count <= 0) return 0;
  return (current + direction + count) % count;
}

export function completionText(command: CommandDefinition): string {
  return command.argument ? `${command.name} ` : command.name;
}

export type CommandKeyAction =
  | { type: "complete" }
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
  if (menuVisible && (key === "Enter" || key === "Tab") && !shiftKey) return { type: "complete" };
  if (key === "Enter" && !shiftKey) return { type: "send" };
  return { type: "none" };
}
