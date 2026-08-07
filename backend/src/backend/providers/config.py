"""Model configuration normalization shared by all provider protocols."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    protocol: str | None = None
    protocol_explicit: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        protocol_explicit = self.protocol is not None
        protocol = str(self.protocol or "chat_completions").strip().lower()
        if protocol == "chat":
            protocol = "chat_completions"
        if protocol not in {"chat_completions", "responses", "messages"}:
            raise ModelConfigurationError("protocol must be chat_completions, responses, or messages")
        if not self.base_url or not self.model:
            raise ModelConfigurationError("base_url and model are required")
        if not 1 <= int(self.max_tokens) <= 384_000 or int(self.context_size) <= int(self.max_tokens):
            raise ModelConfigurationError("Invalid model token limits.")
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "protocol_explicit", protocol_explicit)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> ModelConfig:
        try:
            return cls(
                api_key=str(values.get("api_key") or ""),
                base_url=str(values.get("base_url") or ""),
                model=str(values.get("model") or ""),
                timeout_seconds=int(values.get("timeout_seconds", 45)),
                max_tokens=int(values.get("max_tokens", 8192)),
                provider=str(values.get("provider") or "deepseek"),
                context_size=int(values.get("context_size", 1_024_000)),
                tokenizer_model=str(values.get("tokenizer_model") or "deepseek-ai/DeepSeek-V3"),
                protocol=str(values.get("protocol") or "chat_completions"),
            )
        except (TypeError, ValueError) as exc:
            raise ModelConfigurationError("Model configuration values are invalid.") from exc

    @classmethod
    def from_env(cls, env_path: Path, environ: Mapping[str, str] | None = None) -> ModelConfig:
        values = {**load_env_file(env_path), **dict(os.environ if environ is None else environ)}
        missing = [name for name in ("API_KEY", "BASE_URL", "MODEL") if not values.get(name)]
        if missing:
            raise ModelConfigurationError(f"Missing {', '.join(missing)}. Add it to {env_path.name}.")
        try:
            max_tokens = int(values.get("MAX_TOKENS", "8192"))
            context_size = int(values.get("CONTEXT_SIZE", "1024000"))
        except ValueError as exc:
            raise ModelConfigurationError("MAX_TOKENS and CONTEXT_SIZE must be integers.") from exc
        return cls(
            values["API_KEY"],
            values["BASE_URL"],
            values["MODEL"],
            max_tokens=max_tokens,
            provider=values.get("PROVIDER", "deepseek").strip().lower(),
            context_size=context_size,
            tokenizer_model=values.get("TOKENIZER_MODEL", "deepseek-ai/DeepSeek-V3").strip(),
            protocol=values.get("PROTOCOL", "chat_completions").strip().lower(),
        )

    @classmethod
    def from_toml(cls, config_path: Path) -> ModelConfig:
        from backend.configuration import load_config, section

        values = section(load_config(config_path), "model")
        missing = [name for name in ("api_key", "base_url", "model") if not values.get(name)]
        if missing:
            raise ModelConfigurationError(f"Missing {', '.join(missing)} in [model].")
        try:
            return cls(
                str(values["api_key"]),
                str(values["base_url"]),
                str(values["model"]),
                max_tokens=int(values.get("max_tokens", 8192)),
                provider=str(values.get("provider", "deepseek")).strip().lower(),
                context_size=int(values.get("context_size", 1_024_000)),
                tokenizer_model=str(values.get("tokenizer_model", "deepseek-ai/DeepSeek-V3")).strip(),
                protocol=str(values.get("protocol", "chat_completions")).strip().lower(),
            )
        except (TypeError, ValueError) as exc:
            raise ModelConfigurationError("Invalid [model] configuration.") from exc

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith(("/chat/completions", "/responses", "/messages")):
            return base
        suffix = {
            "chat_completions": "chat/completions",
            "responses": "responses",
            "messages": "messages",
        }[self.protocol]
        if base.endswith("/v1"):
            return f"{base}/{suffix}"
        return f"{base}/v1/{suffix}"
