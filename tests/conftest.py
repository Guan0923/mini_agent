"""PostgreSQL integration-test isolation."""

from __future__ import annotations

import os

import pytest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://mini_agent:mini_agent@127.0.0.1:5432/mini_agent_test",
)
os.environ.setdefault("TEST_DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)


_POSTGRES_TEST_MODULES = {
    "test_plan_message_flow",
    "test_plan_questions",
    "test_resume",
    "test_runtime_context",
    "test_runtime_messages",
    "test_sessions",
    "test_steering",
    "test_time_tools",
    "test_sync_server",
    "test_web_auth_postgres",
}


@pytest.fixture(autouse=True)
def reset_postgres_schema(request: pytest.FixtureRequest) -> None:
    """Isolate only legacy PostgreSQL integration tests.

    Keeping the import and connection local means the local-first client test
    suite can run without installing the optional server dependencies.
    """

    if request.module.__name__.rsplit(".", 1)[-1] not in _POSTGRES_TEST_MODULES:
        return

    import psycopg

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=3) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
