"""PostgreSQL integration-test isolation."""

from __future__ import annotations

import os

import psycopg
import pytest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://mini_agent:mini_agent@localhost:5432/mini_agent_test",
)
os.environ.setdefault("TEST_DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)


@pytest.fixture(autouse=True)
def reset_postgres_schema() -> None:
    """Give every test an empty schema without touching the development database."""

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=3) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
