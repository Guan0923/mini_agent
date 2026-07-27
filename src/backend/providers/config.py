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

    @classmethod
    def from_toml(cls, config_path: Path) -> ModelConfig:
        """Read model settings only from ~/mini_agent/config.toml."""

        from backend.configuration import load_config, section

        values = section(load_config(config_path), "model")
        missing = [name for name in ("api_key", "base_url", "model") if not values.get(name)]
        if missing:
            raise ModelConfigurationError(f"Missing {', '.join(missing)} in [model].")
        try:
            max_tokens = int(values.get("max_tokens", 8192))
            context_size = int(values.get("context_size", 1_024_000))
        except (TypeError, ValueError) as exc:
            raise ModelConfigurationError("model.max_tokens and model.context_size must be integers.") from exc
        if not 1 <= max_tokens <= 384_000 or context_size <= max_tokens:
            raise ModelConfigurationError("Invalid [model] token limits.")
        provider = str(values.get("provider", "deepseek")).strip().lower()
        tokenizer_model = str(values.get("tokenizer_model", "deepseek-ai/DeepSeek-V3")).strip()
        if not provider or not tokenizer_model:
            raise ModelConfigurationError("model.provider and model.tokenizer_model must not be empty.")
        return cls(
            str(values["api_key"]),
            str(values["base_url"]),
            str(values["model"]),
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
