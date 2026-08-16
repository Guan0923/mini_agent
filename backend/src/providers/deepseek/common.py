"""Shared DeepSeek option validation."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from ..errors import ModelRequestError

_PROVIDER = "deepseek"
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_DOCUMENTED_PARAMETERS = {
    "frequency_penalty",
    "logprobs",
    "max_tokens",
    "presence_penalty",
    "reasoning_effort",
    "response_format",
    "stop",
    "stream_options",
    "temperature",
    "thinking",
    "tool_choice",
    "top_logprobs",
    "top_p",
    "user_id",
}
_MANAGED_PARAMETERS = {"messages", "model", "stream", "tools"}


def _provider_options(value: Any) -> dict[str, Any]:
    options = value.provider_options.get(_PROVIDER, {})
    if not isinstance(options, dict):
        raise ModelRequestError("DeepSeek provider_options must be an object.")
    return options


def _merge_extra_fields(
    target: dict[str, Any],
    raw: Any,
    *,
    protected: set[str],
    label: str,
) -> None:
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise ModelRequestError(f"DeepSeek {label} extra_body must be an object.")
    collisions = protected.intersection(raw)
    if collisions:
        fields = ", ".join(sorted(collisions))
        raise ModelRequestError(f"DeepSeek {label} extra_body cannot override: {fields}.")
    target.update(copy.deepcopy(dict(raw)))
