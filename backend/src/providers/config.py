"""Model configuration normalization shared by all provider protocols."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ModelConfigurationError

SUPPORTED_PROTOCOLS = frozenset({"chat_completions", "responses", "messages"})
DEFAULT_PROVIDER_NAME = "default"


def _normalize_provider_name(value: object) -> str:
    name = str(value or DEFAULT_PROVIDER_NAME).strip()
    if not name or name.casefold() == "deepseek":
        return DEFAULT_PROVIDER_NAME
    return name


def _normalize_tokenizer_model(value: object) -> str:
    tokenizer = str(value or "").strip()
    if tokenizer.casefold().startswith("deepseek-ai/"):
        return ""
    return tokenizer


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
    # ``provider`` is retained for runtime/state compatibility.  It is the
    # internal adapter kind and is normalized to ``protocol`` below; the
    # user-facing configuration identity is ``provider_name``.
    provider: str = "chat_completions"
    provider_name: str = DEFAULT_PROVIDER_NAME
    context_size: int = 1_024_000
    tokenizer_model: str = ""
    protocol: str | None = None
    temperature: float = 0.0
    protocol_explicit: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        protocol_explicit = self.protocol is not None
        protocol = str(self.protocol or "chat_completions").strip().lower()
        if protocol == "chat":
            protocol = "chat_completions"
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ModelConfigurationError("protocol must be chat_completions, responses, or messages")
        if not self.base_url or not self.model:
            raise ModelConfigurationError("base_url and model are required")
        if not 1 <= int(self.max_tokens) <= 384_000 or int(self.context_size) <= int(self.max_tokens):
            raise ModelConfigurationError("Invalid model token limits.")
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, int | float)
            or not math.isfinite(self.temperature)
            or not 0 <= self.temperature <= 2
        ):
            raise ModelConfigurationError("temperature must be a finite number between 0 and 2")
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "protocol_explicit", protocol_explicit)
        object.__setattr__(self, "provider", protocol)
        object.__setattr__(self, "provider_name", _normalize_provider_name(self.provider_name))
        object.__setattr__(self, "tokenizer_model", _normalize_tokenizer_model(self.tokenizer_model))
        object.__setattr__(self, "temperature", float(self.temperature))

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> ModelConfig:
        try:
            raw_temperature = values.get("temperature", 0.0)
            if isinstance(raw_temperature, bool):
                raise TypeError("temperature must be numeric")
            return cls(
                api_key=str(values.get("api_key") or ""),
                base_url=str(values.get("base_url") or ""),
                model=str(values.get("model") or ""),
                timeout_seconds=int(values.get("timeout_seconds", 45)),
                max_tokens=int(values.get("max_tokens", 8192)),
                provider=str(values.get("provider") or values.get("protocol") or "chat_completions"),
                provider_name=str(values.get("provider_name") or values.get("provider") or DEFAULT_PROVIDER_NAME),
                context_size=int(values.get("context_size", 1_024_000)),
                tokenizer_model=str(values.get("tokenizer_model") or ""),
                protocol=str(values.get("protocol") or "chat_completions"),
                temperature=float(raw_temperature),
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
            temperature = float(values.get("TEMPERATURE", "0"))
        except ValueError as exc:
            raise ModelConfigurationError("MAX_TOKENS, CONTEXT_SIZE, and TEMPERATURE are invalid.") from exc
        return cls(
            values["API_KEY"],
            values["BASE_URL"],
            values["MODEL"],
            max_tokens=max_tokens,
            provider=values.get("PROVIDER", values.get("PROTOCOL", "chat_completions")).strip().lower(),
            provider_name=values.get("PROVIDER_NAME", values.get("PROVIDER", DEFAULT_PROVIDER_NAME)).strip(),
            context_size=context_size,
            tokenizer_model=values.get("TOKENIZER_MODEL", "").strip(),
            protocol=values.get("PROTOCOL", "chat_completions").strip().lower(),
            temperature=temperature,
        )

    @classmethod
    def from_toml(cls, config_path: Path) -> ModelConfig:
        from backend.configuration import load_config, section

        values = section(load_config(config_path), "model")
        missing = [name for name in ("api_key", "base_url", "model") if not values.get(name)]
        if missing:
            raise ModelConfigurationError(f"Missing {', '.join(missing)} in [model].")
        try:
            raw_temperature = values.get("temperature", 0.0)
            if isinstance(raw_temperature, bool):
                raise TypeError("temperature must be numeric")
            return cls(
                str(values["api_key"]),
                str(values["base_url"]),
                str(values["model"]),
                max_tokens=int(values.get("max_tokens", 8192)),
                provider=str(values.get("provider", values.get("protocol", "chat_completions"))).strip().lower(),
                provider_name=str(values.get("provider_name", values.get("provider", DEFAULT_PROVIDER_NAME))).strip(),
                context_size=int(values.get("context_size", 1_024_000)),
                tokenizer_model=str(values.get("tokenizer_model", "")).strip(),
                protocol=str(values.get("protocol", "chat_completions")).strip().lower(),
                temperature=float(raw_temperature),
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
