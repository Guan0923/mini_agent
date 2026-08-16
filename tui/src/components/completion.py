"""Slash-command completion independent of the terminal UI framework."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .commands import COMMAND_DEFINITIONS, CommandDefinition

_SLASH_TOKEN_PATTERN = re.compile(r"(?<!\S)(/[^\s]*)$")


@dataclass(frozen=True, slots=True)
class CommandSuggestion:
    """One replacement candidate for the slash token at the cursor."""

    value: str
    description: str
    start_position: int


class SlashCommandCompleter:
    """Suggest known slash commands at the start of the current token."""

    def __init__(self, commands: tuple[CommandDefinition, ...] = COMMAND_DEFINITIONS) -> None:
        self._commands = commands

    def suggestions(self, text: str, cursor_position: int) -> list[CommandSuggestion]:
        match = _SLASH_TOKEN_PATTERN.search(text[:cursor_position])
        if match is None:
            return []

        prefix = match.group(1)
        return [
            CommandSuggestion(candidate, command.description, match.start(1))
            for command in self._commands
            if (candidate := f"/{command.name}") != prefix and candidate.startswith(prefix)
        ]
