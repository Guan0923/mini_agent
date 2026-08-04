"""Shared normalization for untrusted web text."""

from __future__ import annotations

import re


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
