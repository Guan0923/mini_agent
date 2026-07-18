"""Shared command definitions and syntax helpers for the terminal UI."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandDefinition:
    """Describe one slash command for parsing, help, and completion."""

    name: str
    usage: str
    description: str


COMMAND_DEFINITIONS = (
    CommandDefinition("agent", "/agent", "Enter normal Agent mode."),
    CommandDefinition("plan", "/plan", "Enter read-only planning and discussion mode."),
    CommandDefinition("permission", "/permission", "Choose the in-memory tool approval mode."),
    CommandDefinition("sessions", "/sessions", "List saved sessions."),
    CommandDefinition("session", "/session", "Show the current session."),
    CommandDefinition("history", "/history", "Open the read-only current-session history view."),
    CommandDefinition("new", "/new <title>", "Clear the terminal and prepare a new session."),
    CommandDefinition("clear", "/clear <title>", "Clear the terminal and prepare a new session."),
    CommandDefinition("use", "/use <session_id>", "Switch to a saved session."),
    CommandDefinition("help", "/help", "Show this help."),
    CommandDefinition("tools", "/tools", "List available tools."),
    CommandDefinition("trace", "/trace", "Print the last run trace as JSON."),
    CommandDefinition("quit", "/quit", "Exit."),
)

COMMAND_NAMES = tuple(command.name for command in COMMAND_DEFINITIONS)
COMMAND_ARGUMENT_NAMES = frozenset({"new", "clear", "use"})
_COMMAND_ALTERNATION = "|".join(re.escape(name) for name in COMMAND_NAMES)
COMMAND_PATTERN = re.compile(rf"(?<!\S)/(?P<name>{_COMMAND_ALTERNATION})(?=\s|$)")


def render_help() -> str:
    """Render the command help text from the shared command catalog."""

    lines = ["Commands:", "  <task>                 Run a task."]
    lines.extend(f"  {command.usage:<22}  {command.description}" for command in COMMAND_DEFINITIONS)
    lines.extend(
        [
            "  @path                  Include a workspace file in the task context.",
            "",
            "Recognized commands run in text order before one merged task.",
            "Commands with arguments use a space, for example: /new Research notes",
        ]
    )
    return "\n".join(lines)
