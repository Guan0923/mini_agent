"""Fail-closed conversation cleaning for memory model inputs and outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass

_INSTRUCTION_BLOCK_RE = re.compile(
    r"<(?P<tag>agent-instruction-chain|agents-md|skill-instructions|skill)\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_INSTRUCTION_MARKER_RE = re.compile(
    r"</?(?:agent-instruction-chain|agents-md|skill-instructions|skill)\b",
    re.IGNORECASE,
)
_INSTRUCTION_HEADING_RE = re.compile(r"(?im)^#{1,6}\s+(?:global|project|skill|agent)\s+instructions?[^\r\n]*\r?\n?")
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(api[_ -]?(?:key|token)|access[_ -]?token|refresh[_ -]?token|authorization|cookie|"
    r"password|passwd|secret|token|credential)\b\s*([=:])\s*(?:bearer\s+)?(?:\"[^\"\r\n]+\"|"
    r"'[^'\r\n]+'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_COMMON_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,})\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


@dataclass(frozen=True)
class MemorySanitizationResult:
    text: str
    redaction_count: int = 0
    removed_instruction_payload: bool = False
    truncated: bool = False


class MemorySanitizer:
    """Remove instruction payloads and redact common reusable credentials."""

    def __init__(self, *, max_bytes: int = 64 * 1024) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer.")
        self.max_bytes = max_bytes

    def sanitize(self, value: str) -> MemorySanitizationResult:
        if not isinstance(value, str):
            raise ValueError("Memory text must be a string.")
        text, instruction_count = _INSTRUCTION_BLOCK_RE.subn("", value)
        text = _INSTRUCTION_HEADING_RE.sub("", text)
        marker = _INSTRUCTION_MARKER_RE.search(text)
        if marker is not None:
            text = text[: marker.start()]
            instruction_count += 1

        redactions = 0
        text, count = _PEM_PRIVATE_KEY_RE.subn("[REDACTED_PRIVATE_KEY]", text)
        redactions += count

        def replace_named(match: re.Match[str]) -> str:
            return f"{match.group(1)}{match.group(2)}[REDACTED]"

        text, count = _NAMED_SECRET_RE.subn(replace_named, text)
        redactions += count
        for pattern in (_BEARER_RE, _COMMON_TOKEN_RE, _JWT_RE):
            text, count = pattern.subn("[REDACTED]", text)
            redactions += count

        text = text.replace("\x00", "")
        encoded = text.encode("utf-8")
        truncated = len(encoded) > self.max_bytes
        if truncated:
            encoded = encoded[: self.max_bytes]
            while encoded:
                try:
                    text = encoded.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    encoded = encoded[:-1]
            else:
                text = ""
        return MemorySanitizationResult(
            text=text.strip(),
            redaction_count=redactions,
            removed_instruction_payload=instruction_count > 0,
            truncated=truncated,
        )


__all__ = ["MemorySanitizationResult", "MemorySanitizer"]
