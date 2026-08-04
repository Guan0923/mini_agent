import type { DisplayMode } from "./types";

export interface CommandDefinition {
  name: string;
  label: string;
  description: string;
  argument?: string;
}

/** Browser equivalents of the TUI command catalog. */
export const COMMANDS: CommandDefinition[] = [
  { name: "/permission", label: "权限", description: "选择工具审批方式" },
  { name: "/display", label: "显示", description: "选择运行详情级别", argument: "minimal|medium|verbose" },
  { name: "/time", label: "时间", description: "选择当前会话时区" },
  { name: "/sessions", label: "会话", description: "列出服务端保存的会话" },
  { name: "/resume", label: "恢复", description: "恢复一个暂停或失败的运行", argument: "session_id" },
  { name: "/fork", label: "分叉", description: "从已完成运行创建新会话", argument: "run_id" },
  { name: "/history", label: "历史", description: "重新加载当前会话历史" },
  { name: "/new", label: "新建", description: "创建新的服务端会话", argument: "title" },
  { name: "/clear", label: "清空", description: "创建新会话并离开当前上下文", argument: "title" },
  { name: "/help", label: "帮助", description: "查看使用说明" },
  { name: "/tools", label: "工具", description: "列出 agent 可用的工具" },
  { name: "/skills", label: "技能", description: "列出已发现的技能" },
  { name: "/compact", label: "压缩", description: "压缩当前会话上下文" },
  { name: "/trace", label: "追踪", description: "查看最近一次运行的只读追踪" },
  { name: "/benchmark", label: "成绩单", description: "打开 Benchmark 成绩单页" },
];

export const DISPLAY_LEVELS: DisplayMode[] = ["minimal", "medium", "verbose"];

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
  "`/agent` 和 `/plan` 仍可作为兼容别名切换模式，但不会出现在命令菜单中。",
  "发送方式：`Enter` 发送，`Shift+Enter` 换行。",
].join("\n");

export interface ParsedCommand {
  name: string;
  argument: string;
}

const COMMAND_NAMES = new Set([
  ...COMMANDS.map((command) => command.name),
  "/agent",
  "/plan",
]);

export function parseCommand(input: string): ParsedCommand | null {
  const match = input.trim().match(/^(\/[^\s]+)(?:\s+([\s\S]*))?$/);
  if (!match || !COMMAND_NAMES.has(match[1].toLowerCase())) return null;
  return { name: match[1].toLowerCase(), argument: (match[2] ?? "").trim() };
}

export function isArgumentCommand(command: CommandDefinition): boolean {
  return Boolean(command.argument);
}
