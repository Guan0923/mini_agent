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
    CommandDefinition("display", "/display <minimal|medium|verbose>", "Set TUI transcript detail level."),
    CommandDefinition("sessions", "/sessions", "List saved sessions."),
    CommandDefinition("resume", "/resume <session_id>", "Resume the current, latest, or selected session."),
    CommandDefinition("history", "/history", "Open the read-only current-session history view."),
    CommandDefinition("new", "/new <title>", "Clear the terminal and prepare a new session."),
    CommandDefinition("clear", "/clear <title>", "Clear the terminal and prepare a new session."),
    CommandDefinition("help", "/help", "Show this help."),
    CommandDefinition("tools", "/tools", "List available tools."),
    CommandDefinition("skills", "/skills", "List discovered project Skills."),
    CommandDefinition("compact", "/compact", "Compact old conversation context now."),
    CommandDefinition("trace", "/trace", "Open the read-only last-run trace view."),
    CommandDefinition("quit", "/quit", "Exit."),
)

COMMAND_NAMES = tuple(command.name for command in COMMAND_DEFINITIONS)
COMMAND_ARGUMENT_NAMES = frozenset({"new", "clear", "resume", "display"})
_COMMAND_ALTERNATION = "|".join(re.escape(name) for name in COMMAND_NAMES)
COMMAND_PATTERN = re.compile(rf"(?<!\S)/(?P<name>{_COMMAND_ALTERNATION})(?=\s|$)")


def render_help() -> str:
    """Render the command help text from the shared command catalog."""

    lines = ["Commands:", "  <task>                 Run a task."]
    lines.extend(f"  {command.usage:<22}  {command.description}" for command in COMMAND_DEFINITIONS)
    lines.extend(
        [
            "  @path                  Include a workspace file in the task context.",
            "  $name                  Explicitly activate a discovered project Skill.",
            "",
            "Recognized commands run in text order before one merged task.",
            "Commands with arguments use a space, for example: /new Research notes",
        ]
    )
    return "\n".join(lines)
