"""Prompt-toolkit completion for terminal slash commands."""

from __future__ import annotations

import re
from collections.abc import Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from .commands import COMMAND_DEFINITIONS, CommandDefinition

_SLASH_TOKEN_PATTERN = re.compile(r"(?<!\S)(/[^\s]*)$")


class SlashCommandCompleter(Completer):
    """Suggest known slash commands at the start of a token."""

    def __init__(self, commands: tuple[CommandDefinition, ...] = COMMAND_DEFINITIONS) -> None:
        self._commands = commands

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        del complete_event
        match = _SLASH_TOKEN_PATTERN.search(document.text_before_cursor)
        if match is None:
            return

        prefix = match.group(1)
        for command in self._commands:
            candidate = f"/{command.name}"
            if candidate == prefix or not candidate.startswith(prefix):
                continue
            yield Completion(
                candidate,
                start_position=-len(prefix),
                display=candidate,
                display_meta=command.description,
            )
