import sqlite3
from pathlib import Path

from mini_agent.domain import AgentAction, RunState, StrategySelection
from mini_agent.runtime import AgentRunner, SQLiteCheckpointStore, SQLiteSessionStore
from mini_agent.tools import ToolRegistry
from mini_agent.tui.cli import TerminalApp


def test_sqlite_session_store_persists_multi_turn_conversation(tmp_path: Path) -> None:
    database = tmp_path / ".mini_agent" / "checkpoints.db"
    store = SQLiteSessionStore(database)
    session = store.create_session()

    store.start_turn(session.session_id, "run_one", "Remember that I like Python.")
    store.finish_turn(session.session_id, "run_one", "completed", "I will remember that.")
    store.start_turn(session.session_id, "run_two", "What do I like?")
    store.finish_turn(session.session_id, "run_two", "completed", "You like Python.")

    reopened = SQLiteSessionStore(database)
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
    store = SQLiteSessionStore(tmp_path / "checkpoints.db")
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
    database = tmp_path / ".mini_agent" / "checkpoints.db"
    checkpoints = SQLiteCheckpointStore(database)
    state = RunState(task="checkpointed", mode="agent")
    checkpoints.save(state, "run_started")

    sessions = SQLiteSessionStore(database)
    session = sessions.create_session()
    sessions.start_turn(session.session_id, state.run_id, state.task)
    sessions.finish_turn(session.session_id, state.run_id, "completed", "done")

    assert checkpoints.load(state.run_id) is not None
    assert sessions.load_conversation(session.session_id) == [
        {"role": "user", "content": "checkpointed"},
        {"role": "assistant", "content": "done"},
    ]


def test_session_store_migrates_legacy_message_uniqueness(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE session_runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (run_id, role)
            );
            INSERT INTO sessions VALUES ('session_legacy', 'Legacy', 'now', 'now');
            INSERT INTO session_runs VALUES ('run_legacy', 'session_legacy', 'start', 'running', 'now', 'now');
            INSERT INTO session_messages (session_id, run_id, role, content, created_at)
            VALUES ('session_legacy', 'run_legacy', 'user', 'start', 'now');
            """
        )

    store = SQLiteSessionStore(database)
    store.append_turn_input("session_legacy", "run_legacy", "steer")
    store.finish_turn("session_legacy", "run_legacy", "completed", "done")

    assert store.load_conversation("session_legacy") == [
        {"role": "user", "content": "start"},
        {"role": "user", "content": "steer"},
        {"role": "assistant", "content": "done"},
    ]


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
    assert TerminalApp._split_input("/clear Reset /use session_123") == [
        ("command", "clear", "Reset"),
        ("command", "use", "session_123"),
    ]
    assert TerminalApp._split_input("/use session_123 trailing text") == [
        ("command", "use", "session_123 trailing text"),
    ]
    assert TerminalApp._split_input("/new/legacy title") == [("task", "/new/legacy title", "")]
    assert TerminalApp._split_input("/use/session_123") == [("task", "/use/session_123", "")]


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
