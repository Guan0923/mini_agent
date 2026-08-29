"""Packaged system-prompt loading and composition."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from backend.domain import PlanningError

_MODE_SLOT = "{{MODE_PROMPT}}"
_PROMPT_NAMES = ("instruction", "default", "plan", "agent")


class PromptConfigurationError(PlanningError):
    """Raised when packaged prompt resources cannot form a valid system prompt."""


@dataclass(frozen=True)
class PromptTemplates:
    """The four templates used by the interactive decision loop."""

    instruction: str
    shared: str
    plan: str
    agent: str

    def compose(self, mode: str) -> str:
        """Embed the selected mode in the shared template and prepend project instructions."""

        if mode not in {"agent", "plan"}:
            raise PromptConfigurationError(f"Unsupported prompt mode: {mode!r}.")
        values = {
            "instruction": self.instruction,
            "default": self.shared,
            "plan": self.plan,
            "agent": self.agent,
        }
        empty = [name for name, value in values.items() if not value.strip()]
        if empty:
            raise PromptConfigurationError(f"Prompt template must not be empty: {', '.join(empty)}.")
        slot_count = self.shared.count(_MODE_SLOT)
        if slot_count != 1:
            raise PromptConfigurationError(
                f"Default prompt must contain {_MODE_SLOT!r} exactly once; found {slot_count}."
            )
        mode_prompt = self.plan if mode == "plan" else self.agent
        if _MODE_SLOT in self.instruction or _MODE_SLOT in mode_prompt:
            raise PromptConfigurationError("Only the default prompt may contain the mode placeholder.")
        shared = self.shared.replace(_MODE_SLOT, mode_prompt)
        return f"{self.instruction.strip()}\n\n{shared.strip()}"


def _read_prompt(name: str) -> str:
    if name not in _PROMPT_NAMES:
        raise PromptConfigurationError(f"Unknown prompt resource: {name!r}.")
    resource = files(__package__).joinpath(f"{name}.md")
    try:
        content = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise PromptConfigurationError(f"Prompt resource is unavailable: {name}.md") from exc
    if not content.strip():
        raise PromptConfigurationError(f"Prompt resource must not be empty: {name}.md")
    return content.strip()


@lru_cache(maxsize=1)
def load_prompt_templates() -> PromptTemplates:
    """Load the installed prompt resources once per process."""

    return PromptTemplates(
        instruction=_read_prompt("instruction"),
        shared=_read_prompt("default"),
        plan=_read_prompt("plan"),
        agent=_read_prompt("agent"),
    )


def compose_system_prompt(mode: str) -> str:
    """Return the complete main-loop system prompt for one runtime mode."""

    return load_prompt_templates().compose(mode)
