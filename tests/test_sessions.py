import os
from pathlib import Path

import psycopg

from backend.domain import AgentAction, RunState, StrategySelection
from backend.runtime import AgentRunner, PostgresCheckpointStore, PostgresSessionStore
from backend.tools import ToolRegistry
from tui.cli import TerminalApp


def test_postgres_session_store_persists_multi_turn_conversation(tmp_path: Path) -> None:
    store = PostgresSessionStore()
    session = store.create_session()

    store.start_turn(session.session_id, "run_one", "Remember that I like Python.")
    store.finish_turn(session.session_id, "run_one", "completed", "I will remember that.")
    store.start_turn(session.session_id, "run_two", "What do I like?")
    store.finish_turn(session.session_id, "run_two", "completed", "You like Python.")

    reopened = PostgresSessionStore()
    assert reopened.load_conversation(session.session_id) == [
        {"role": "user", "content": "Remember that I like Python."},
        {"role": "assistant", "content": "I will remember that."},
        {"role": "user", "content": "What do I like?"},
        {"role": "assistant", "content": "You like Python."},
    ]
    summary = reopened.get_session_summary(session.session_id)
    assert summary is not None
    assert summary.title == "Remember that I like Python."
    assert summary.message_count == 4
    assert summary.last_run_id == "run_two"
    assert summary.last_run_status == "completed"


def test_session_store_finish_is_idempotent_and_persists_terminal_messages(tmp_path: Path) -> None:
    store = PostgresSessionStore()
    session = store.create_session("Test session")

    store.start_turn(session.session_id, "run_failed", "This will fail.")
    store.finish_turn(session.session_id, "run_failed", "failed", "The tool failed.")
    store.finish_turn(session.session_id, "run_failed", "failed", "The tool failed again.")
    store.start_turn(session.session_id, "run_cancelled", "Cancel this.")
    store.finish_turn(session.session_id, "run_cancelled", "cancelled", None)

    assert store.load_conversation(session.session_id) == [
        {"role": "user", "content": "This will fail."},
        {"role": "assistant", "content": "The tool failed again."},
        {"role": "user", "content": "Cancel this."},
        {"role": "assistant", "content": "Task cancelled by user."},
    ]


def test_session_store_shares_database_with_existing_checkpoints(tmp_path: Path) -> None:
    checkpoints = PostgresCheckpointStore()
    state = RunState(task="checkpointed", mode="agent")
    checkpoints.save(state, "run_started")

    sessions = PostgresSessionStore()
    session = sessions.create_session()
    sessions.start_turn(session.session_id, state.run_id, state.task)
    sessions.finish_turn(session.session_id, state.run_id, "completed", "done")

    assert checkpoints.load(state.run_id) is not None
    assert sessions.load_conversation(session.session_id) == [
        {"role": "user", "content": "checkpointed"},
        {"role": "assistant", "content": "done"},
    ]


def test_postgres_schema_initialization_is_idempotent(tmp_path: Path) -> None:
    first_store = PostgresSessionStore()
    second_store = PostgresSessionStore()
    session = first_store.create_session("Schema")
    first_store.start_turn(session.session_id, "run_schema", "start")
    second_store.append_turn_input(session.session_id, "run_schema", "steer")
    second_store.finish_turn(session.session_id, "run_schema", "completed", "done")

    assert second_store.load_conversation(session.session_id) == [
        {"role": "user", "content": "start"},
        {"role": "user", "content": "steer"},
        {"role": "assistant", "content": "done"},
    ]
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as connection:
        columns = {
            row[0]
            for row in connection.execute(
                """SELECT column_name FROM information_schema.columns
                WHERE table_name = 'session_runs'"""
            ).fetchall()
        }
        lineage = connection.execute(
            "SELECT workflow_id, attempt, origin_kind FROM session_runs WHERE run_id = %s",
            ("run_schema",),
        ).fetchone()
    assert {"workflow_id", "attempt", "origin_kind", "source_session_id", "source_run_id"} <= columns
    assert lineage == ("run_schema", 1, "legacy")


class HistoryPlanner:
    name = "history-test"

    def __init__(self) -> None:
        self.histories: list[list[dict[str, str]]] = []

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The test uses one direct answer.")

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        self.histories.append(list(history))
        return AgentAction(type="final_answer", answer=f"history has {len(history)} messages")


def test_tui_consumes_only_spaced_session_arguments_and_rejects_legacy_slash_forms() -> None:
    assert TerminalApp._split_input("before /new Project notes /plan after") == [
        ("command", "new", "Project notes"),
        ("command", "plan", ""),
        ("task", "before after", ""),
    ]
    assert TerminalApp._split_input("/resume session_123") == [
        ("command", "resume", "session_123"),
    ]
    assert TerminalApp._split_input("/resume") == [("command", "resume", "")]
    assert TerminalApp._split_input("/use session_123") == [("command", "legacy_session", "")]
    assert TerminalApp._split_input("/session") == [("command", "legacy_session", "")]
    assert TerminalApp._split_input("/new/legacy title") == [("task", "/new/legacy title", "")]
    assert TerminalApp._split_input("/resume/session_123") == [("task", "/resume/session_123", "")]


def test_tui_quit_stops_line_before_submitting_a_task(tmp_path: Path, monkeypatch) -> None:
    app = TerminalApp(AgentRunner(HistoryPlanner(), ToolRegistry(tmp_path)))
    app.mode = "plan"
    tasks: list[str] = []
    monkeypatch.setattr(app, "run_task", tasks.append)

    assert app._handle("before /agent /quit after /plan") is False
    assert app.mode == "agent"
    assert tasks == []


def test_tui_does_not_treat_paths_or_urls_as_commands() -> None:
    segments = TerminalApp._split_input("read docs/architecture.md from https://example.com/guide")

    assert segments == [("task", "read docs/architecture.md from https://example.com/guide", "")]


def test_postgres_stores_reuse_the_process_connection_pool() -> None:
    first = PostgresSessionStore()
    second = PostgresSessionStore()

    assert first._database is second._database


def test_session_store_reads_conversation_in_chronological_cursor_pages() -> None:
    store = PostgresSessionStore()
    session = store.create_session("Paged")
    for index in range(3):
        run_id = f"run_page_{index}"
        store.start_turn(session.session_id, run_id, f"question {index}")
        store.finish_turn(session.session_id, run_id, "completed", f"answer {index}")

    newest, before_id = store.load_conversation_page(session.session_id, limit=2)
    older, final_before_id = store.load_conversation_page(session.session_id, before_id=before_id, limit=2)
    oldest, exhausted_before_id = store.load_conversation_page(session.session_id, before_id=final_before_id, limit=2)

    assert newest == [
        {"role": "user", "content": "question 2"},
        {"role": "assistant", "content": "answer 2"},
    ]
    assert older == [
        {"role": "user", "content": "question 1"},
        {"role": "assistant", "content": "answer 1"},
    ]
    assert oldest == [
        {"role": "user", "content": "question 0"},
        {"role": "assistant", "content": "answer 0"},
    ]
    assert exhausted_before_id is None
