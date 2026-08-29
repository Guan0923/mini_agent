
export interface CommandDefinition {
  name: string;
  label: string;
  description: string;
  argument?: string;
}

/** Browser equivalents of the TUI command catalog. */
export const COMMANDS: CommandDefinition[] = [
  { name: "/new", label: "新建", description: "创建新的服务端会话", argument: "title" },
  { name: "/init", label: "初始化", description: "在当前项目根目录创建 AGENTS.md" },
  { name: "/help", label: "帮助", description: "查看使用说明" },
  { name: "/skills", label: "技能", description: "列出已发现的技能" },
  { name: "/compact", label: "压缩", description: "压缩当前会话上下文" },
];


export const HELP_TEXT = [
  "# 使用说明",
  "",
  "向 Mini-Agent 输入任务，它会自动调用文件、Shell、Web 等工具完成任务。",
  "",
  "**模式选择：**",
  "- Agent：允许执行工具和修改工作区。",
  "- Plan：只读规划和讨论，提交计划后可进入 Plan Review。",
  "",
  "**斜杠命令：**",
  ...COMMANDS.map((command) => `- \`${command.name}${command.argument ? ` ${command.argument}` : ""}\` ${command.description}`),
  "",
  "发送方式：`Enter` 发送，`Shift+Enter` 换行。",
].join("\n");

export interface ParsedCommand {
  name: string;
  argument: string;
}

const COMMAND_NAMES = new Set(COMMANDS.map((command) => command.name));

export function parseCommand(input: string): ParsedCommand | null {
  const match = input.trim().match(/^(\/[^\s]+)(?:\s+([\s\S]*))?$/);
  if (!match || !COMMAND_NAMES.has(match[1].toLowerCase())) return null;
  return { name: match[1].toLowerCase(), argument: (match[2] ?? "").trim() };
}

export function isArgumentCommand(command: CommandDefinition): boolean {
  return Boolean(command.argument);
}
