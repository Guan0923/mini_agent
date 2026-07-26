"""Loading model settings without coupling callers to a dotenv dependency."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ModelConfigurationError


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


@dataclass(frozen=True)
class ModelConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: int = 45
    max_tokens: int = 8192
    provider: str = "deepseek"
    context_size: int = 1_024_000
    tokenizer_model: str = "deepseek-ai/DeepSeek-V3"

    @classmethod
    def from_env(cls, env_path: Path, environ: Mapping[str, str] | None = None) -> ModelConfig:
        values = {**load_env_file(env_path), **dict(os.environ if environ is None else environ)}
        missing = [name for name in ("API_KEY", "BASE_URL", "MODEL") if not values.get(name)]
        if missing:
            raise ModelConfigurationError(f"Missing {', '.join(missing)}. Add it to {env_path.name}.")
        raw_max_tokens = values.get("MAX_TOKENS", "8192")
        try:
            max_tokens = int(raw_max_tokens)
        except ValueError as exc:
            raise ModelConfigurationError("MAX_TOKENS must be an integer.") from exc
        if not 1 <= max_tokens <= 384_000:
            raise ModelConfigurationError("MAX_TOKENS must be between 1 and 384000.")
        raw_context_size = values.get("CONTEXT_SIZE", "1024000")
        try:
            context_size = int(raw_context_size)
        except ValueError as exc:
            raise ModelConfigurationError("CONTEXT_SIZE must be an integer.") from exc
        if context_size <= max_tokens:
            raise ModelConfigurationError("CONTEXT_SIZE must be greater than MAX_TOKENS.")
        provider = values.get("PROVIDER", "deepseek").strip().lower()
        if not provider:
            raise ModelConfigurationError("PROVIDER must not be empty.")
        tokenizer_model = values.get("TOKENIZER_MODEL", "deepseek-ai/DeepSeek-V3").strip()
        if not tokenizer_model:
            raise ModelConfigurationError("TOKENIZER_MODEL must not be empty.")
        return cls(
            values["API_KEY"],
            values["BASE_URL"],
            values["MODEL"],
            max_tokens=max_tokens,
            provider=provider,
            context_size=context_size,
            tokenizer_model=tokenizer_model,
        )

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"
