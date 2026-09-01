"""Stable message queue facade separating Redis, memory, and mailbox responsibilities."""

from .memory_message_queue import MemoryMessageQueue
from .message_mailbox import RedisAgentMailbox, RedisTurnMailbox
from .message_queue_support import (
    CONSUMER_GROUP,
    DEFAULT_KEY_PREFIX,
    DEFAULT_REDIS_URL,
    DELIVERY_RECEIPT_TTL_SECONDS,
    STALE_CLAIM_MS,
)
from .redis_message_queue import RedisMessageQueue

__all__ = [
    "CONSUMER_GROUP",
    "DEFAULT_KEY_PREFIX",
    "DEFAULT_REDIS_URL",
    "DELIVERY_RECEIPT_TTL_SECONDS",
    "MemoryMessageQueue",
    "RedisAgentMailbox",
    "RedisMessageQueue",
    "RedisTurnMailbox",
    "STALE_CLAIM_MS",
]
