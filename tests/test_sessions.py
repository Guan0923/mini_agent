import os
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


def test_tui_loads_session_conversation_after_restart(tmp_path: Path) -> None:
    database = tmp_path / ".mini_agent" / "checkpoints.db"
    planner = HistoryPlanner()
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    first_store = SQLiteSessionStore(database)
    first_app = TerminalApp(runner, session_store=first_store)

    first_app.run_task("first message")
    assert first_app.active_session is not None
    session_id = first_app.active_session.session_id

    second_store = SQLiteSessionStore(database)
    second_app = TerminalApp(runner, session_store=second_store, session_id=session_id)
    second_app.run_task("second message")

    assert planner.histories[-1][-3:] == [
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "history has 1 messages"},
        {"role": "user", "content": "second message"},
    ]
    assert second_store.get_session(session_id) is not None


def test_tui_new_clear_and_use_switch_conversations_without_resetting_process_settings(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    store = SQLiteSessionStore(tmp_path / "checkpoints.db")
    runner = AgentRunner(HistoryPlanner(), ToolRegistry(tmp_path))
    app = TerminalApp(runner, session_store=store)
    events: list[tuple[str, str | None]] = []
    prepare_new_session = app._conversation_service.prepare_new_session

    monkeypatch.setattr(
        "mini_agent.tui.cli.os.system",
        lambda command: events.append(("clear", command)) or 0,
    )

    def record_pending_session(title: str | None = None) -> None:
        events.append(("pending", title))
        prepare_new_session(title)

    monkeypatch.setattr(app._conversation_service, "prepare_new_session", record_pending_session)

    assert app._handle("/new titled session") is True
    assert events == [("clear", "cls" if os.name == "nt" else "clear"), ("pending", "titled session")]
    assert app.active_session is None
    assert app._conversation_service.pending_session_title == "titled session"
    assert app.conversation == []
    assert store.list_sessions() == []
    assert SQLiteSessionStore(tmp_path / "checkpoints.db").list_sessions() == []

    capsys.readouterr()
    assert app._handle("/session") is True
    assert capsys.readouterr().out == ("SESSION PENDING\nTITLE titled session\nMESSAGES 0\nSTATUS Not saved yet\n")

    app.run_task("first session task")
    assert app.last_state is not None
    assert app.active_session is not None
    first_id = app.active_session.session_id
    assert app.active_session.title == "titled session"
    assert app._conversation_service.pending_session_title is None
    assert len(store.list_sessions()) == 1
    app.mode = "plan"
    app._approval._permission_mode = "full_access"

    assert app._handle("/clear cleared session") is True
    assert events[-2:] == [
        ("clear", "cls" if os.name == "nt" else "clear"),
        ("pending", "cleared session"),
    ]
    assert app.active_session is None
    assert app._conversation_service.pending_session_title == "cleared session"
    assert app.conversation == []
    assert app.last_state is None
    assert app.mode == "plan"
    assert app._approval.permission_mode == "full_access"
    assert store.load_conversation(first_id)
    assert len(store.list_sessions()) == 1

    capsys.readouterr()
    assert app._handle("/history") is True
    assert capsys.readouterr().out == "No conversation history.\n"
    assert app._handle("/sessions") is True
    sessions_output = capsys.readouterr().out
    assert first_id in sessions_output
    assert "cleared session" not in sessions_output

    assert app._handle(f"/use {first_id}") is True
    assert app.active_session is not None
    assert app.active_session.session_id == first_id
    assert app._conversation_service.pending_session_title is None
    assert app.conversation
    assert "SESSION" in capsys.readouterr().out


def test_repeated_pending_sessions_persist_only_after_the_first_task(tmp_path: Path, monkeypatch) -> None:
    store = SQLiteSessionStore(tmp_path / "checkpoints.db")
    app = TerminalApp(AgentRunner(HistoryPlanner(), ToolRegistry(tmp_path)), session_store=store)
    monkeypatch.setattr("mini_agent.tui.cli.os.system", lambda _command: 0)

    assert app._handle("/new discarded title") is True
    assert app._handle("/clear") is True
    assert app.active_session is None
    assert app._conversation_service.pending_session_title == "New session"
    assert store.list_sessions() == []

    app.run_task("first persisted task")

    assert app.active_session is not None
    assert app.active_session.title == "first persisted task"
    assert app._conversation_service.pending_session_title is None
    assert len(store.list_sessions()) == 1


def test_tui_history_shows_only_current_session_messages(tmp_path: Path, capsys) -> None:
    store = SQLiteSessionStore(tmp_path / "checkpoints.db")
    runner = AgentRunner(HistoryPlanner(), ToolRegistry(tmp_path))
    app = TerminalApp(runner, session_store=store)

    app.run_task("first history message")
    capsys.readouterr()

    assert app._handle("/history") is True
    output = capsys.readouterr().out
    assert "HISTORY" in output
    assert "USER\nfirst history message" in output
    assert "ASSISTANT\nhistory has 1 messages" in output
    assert "thinking" not in output.lower()


def test_tui_executes_commands_before_one_merged_task(tmp_path: Path, capsys, monkeypatch) -> None:
    app = TerminalApp(
        AgentRunner(HistoryPlanner(), ToolRegistry(tmp_path)),
        session_store=SQLiteSessionStore(tmp_path / "checkpoints.db"),
    )
    tasks: list[str] = []
    monkeypatch.setattr(app, "run_task", tasks.append)

    assert app._handle("task before /history task after /agent") is True
    output = capsys.readouterr().out
    assert output.index("No active session.") < output.index("Agent mode enabled.")
    assert tasks == ["task before task after"]

    for value in ("你好 /plan", "/plan 你好"):
        app.mode = "agent"
        tasks.clear()

        assert app._handle(value) is True
        assert app.mode == "plan"
        assert tasks == ["你好"]


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


def test_tui_expands_file_reference_before_agent_run(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("remember alpha", encoding="utf-8")
    store = SQLiteSessionStore(tmp_path / "checkpoints.db")
    planner = HistoryPlanner()
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    app = TerminalApp(runner, session_store=store)

    app.run_task("summarize @notes.md")

    expanded = planner.histories[-1][-1]["content"]
    assert "[Referenced file: notes.md]" in expanded
    assert "remember alpha" in expanded
    assert app.conversation[0]["content"] == expanded
