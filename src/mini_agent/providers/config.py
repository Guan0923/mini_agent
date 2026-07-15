"""Loading model settings without coupling callers to a dotenv dependency."""

from __future__ import annotations

import os
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
            os.environ.setdefault(key, value)
    return values


@dataclass(frozen=True)
class ModelConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: int = 45
    max_tokens: int = 8192

    @classmethod
    def from_env(cls, env_path: Path) -> "ModelConfig":
        load_env_file(env_path)
        missing = [name for name in ("API_KEY", "BASE_URL", "MODEL") if not os.environ.get(name)]
        if missing:
            raise ModelConfigurationError(f"Missing {', '.join(missing)}. Add it to {env_path.name}.")
        raw_max_tokens = os.environ.get("MAX_TOKENS", "8192")
        try:
            max_tokens = int(raw_max_tokens)
        except ValueError as exc:
            raise ModelConfigurationError("MAX_TOKENS must be an integer.") from exc
        if not 1 <= max_tokens <= 384_000:
            raise ModelConfigurationError("MAX_TOKENS must be between 1 and 384000.")
        return cls(os.environ["API_KEY"], os.environ["BASE_URL"], os.environ["MODEL"], max_tokens=max_tokens)

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"
