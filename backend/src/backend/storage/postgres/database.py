"""Shared PostgreSQL connection-pool ownership for durable storage."""

from __future__ import annotations

import atexit
import os
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout


class PostgresDatabase:
    """Own a small synchronous pool shared by session and checkpoint adapters."""

    _shared: dict[str, PostgresDatabase] = {}
    _shared_lock = Lock()

    @classmethod
    def shared(cls, database_url: str | None = None) -> PostgresDatabase:
        """Return one process-local pool for convenience-constructed adapters."""

        configured_url = database_url or os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
        if not configured_url.strip():
            raise ValueError("DATABASE_URL must be configured for PostgreSQL storage.")
        with cls._shared_lock:
            database = cls._shared.get(configured_url)
            if database is None:
                database = cls(configured_url)
                cls._shared[configured_url] = database
            return database

    @classmethod
    def close_shared(cls) -> None:
        """Close convenience pools during interpreter shutdown."""

        with cls._shared_lock:
            databases = list(cls._shared.values())
            cls._shared.clear()
        for database in databases:
            database.close()

    def __init__(self, database_url: str | None = None) -> None:
        configured_url = database_url or os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
        if not configured_url.strip():
            raise ValueError("DATABASE_URL must be configured for PostgreSQL storage.")
        self.database_url = configured_url
        self._pool = ConnectionPool(
            conninfo=configured_url,
            min_size=1,
            max_size=4,
            timeout=5,
            open=True,
            kwargs={"autocommit": False, "connect_timeout": 3},
        )

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        """Yield one transaction, translating connection failures for callers."""

        try:
            with self._pool.connection() as connection:
                try:
                    yield connection
                except Exception:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
        except (psycopg.OperationalError, PoolTimeout) as exc:
            raise RuntimeError("Unable to connect to PostgreSQL using DATABASE_URL.") from exc

    def close(self) -> None:
        """Release pooled connections when the owning application exits."""

        self._pool.close()


atexit.register(PostgresDatabase.close_shared)
