
export interface CommandDefinition {
  name: string;
  label: string;
  description: string;
}

/** Commands supported by the browser client. */
export const COMMANDS: CommandDefinition[] = [
  { name: "/help", label: "帮助", description: "查看使用说明" },
  { name: "/skills", label: "技能", description: "列出已发现的技能" },
  { name: "/compact", label: "压缩", description: "压缩当前会话上下文" },
  { name: "/trace", label: "审计", description: "打开当前 Thread 的 Turn Trace" },
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
  ...COMMANDS.map((command) => `- \`${command.name}\` ${command.description}`),
  "",
  "发送方式：`Enter` 发送，`Shift+Enter` 换行。",
].join("\n");

export interface ParsedCommand {
  name: string;
  argument: string;
}

const COMMAND_NAMES = new Set(COMMANDS.map((command) => command.name));

export function parseCommand(input: string): ParsedCommand | null {
  const match = input.trim().match(/^(\/[^\s]+)$/);
  if (!match || !COMMAND_NAMES.has(match[1].toLowerCase())) return null;
  return { name: match[1].toLowerCase(), argument: "" };
}
