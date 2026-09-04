"""Shared constants and canonical serialization for message queue implementations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace

from backend.domain.message_queue import MessageEnvelope, QueuedMessage

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_KEY_PREFIX = "mini-agent:v1"
DELIVERY_RECEIPT_TTL_SECONDS = 7 * 24 * 60 * 60
STALE_CLAIM_MS = 60_000
CONSUMER_GROUP = "runtime"
TURN_START_CONSUMER_GROUP = "turn-start-runtime"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(thread_id: str, turn_id: str, message_ids: Sequence[str]) -> str:
    raw = _json({"thread_id": thread_id, "turn_id": turn_id, "message_ids": sorted(message_ids)})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _envelope_fingerprint(envelope: MessageEnvelope) -> str:
    canonical = replace(envelope, attempts=0).to_dict()
    return hashlib.sha256(_json(canonical).encode("utf-8")).hexdigest()


def _merge(messages: Sequence[QueuedMessage]) -> tuple[str, tuple[dict[str, str], ...]]:
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for message in messages:
        for reference in message.references:
            key = (str(reference.get("source") or ""), str(reference.get("path") or ""))
            if key not in seen:
                seen.add(key)
                references.append(dict(reference))
    return "\n\n".join(message.content for message in messages), tuple(references)


def _same_create(left: QueuedMessage, right: QueuedMessage) -> bool:
    return (
        left.id == right.id
        and left.thread_id == right.thread_id
        and left.content == right.content
        and left.references == right.references
    )
