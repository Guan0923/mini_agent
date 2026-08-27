import sqlite3
from pathlib import Path

from backend.domain import AgentAction, RunState
from backend.runtime import AgentRunner
from backend.sandbox import ApprovalStore
from backend.tools import ToolRegistry
from tests.local_store import session_store


def test_sqlite_session_store_persists_multi_turn_conversation(tmp_path: Path) -> None:
    store = session_store(tmp_path / "store")
    session = store.create_session()

    store.start_turn(session.session_id, "run_one", "Remember that I like Python.")
    store.finish_turn(session.session_id, "run_one", "completed", "I will remember that.")
    store.start_turn(session.session_id, "run_two", "What do I like?")
    store.finish_turn(session.session_id, "run_two", "completed", "You like Python.")

    reopened = session_store(tmp_path / "store")
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
    store = session_store(tmp_path / "store")
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
    checkpoints = session_store(tmp_path / "store")
    state = RunState(task="checkpointed", mode="agent")
    session = checkpoints.create_session()
    runner = AgentRunner(HistoryPlanner(), ToolRegistry())
    runtime = runner.new_runtime(
        task=state.task,
        session_id=session.session_id,
        run_id=state.run_id,
        runtime_store=checkpoints,
    )
    checkpoints.save(runtime, "run_started")

    sessions = session_store(tmp_path / "store")
    sessions.start_turn(session.session_id, state.run_id, state.task)
    sessions.finish_turn(session.session_id, state.run_id, "completed", "done")

    restored = checkpoints.load_runtime(session.session_id)
    assert restored is not None and restored.current_run is not None
    assert restored.current_run.run_id == state.run_id
    assert sessions.load_conversation(session.session_id) == [
        {"role": "user", "content": "checkpointed"},
        {"role": "assistant", "content": "done"},
    ]


def test_sqlite_schema_initialization_is_idempotent(tmp_path: Path) -> None:
    first_store = session_store(tmp_path / "store")
    second_store = session_store(tmp_path / "store")
    session = first_store.create_session("Schema")
    first_store.start_turn(session.session_id, "run_schema", "start")
    second_store.append_turn_input(session.session_id, "run_schema", "steer")
    second_store.finish_turn(session.session_id, "run_schema", "completed", "done")

    assert second_store.load_conversation(session.session_id) == [
        {"role": "user", "content": "start"},
        {"role": "user", "content": "steer"},
        {"role": "assistant", "content": "done"},
    ]
    with sqlite3.connect(first_store.paths.session_db(session.session_id)) as connection:
        lineage = connection.execute(
            "SELECT json_extract(payload_json,'$.workflow_id'), "
            "json_extract(payload_json,'$.attempt'), json_extract(payload_json,'$.origin_kind') "
            "FROM json_objects WHERE namespace='run' AND object_id=?",
            ("run_schema",),
        ).fetchone()
    assert lineage == ("run_schema", 1, "legacy")


def test_session_sandbox_approval_is_local_and_persistent(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = session_store(root)
    session = store.create_session("Approval")
    ApprovalStore(store).decide(
        session_id=session.session_id,
        command="git status --short",
        cwd="C:\\workspace",
        permission_target="workspace_write",
        decision="allow_session",
    )

    reopened = session_store(root)
    assert ApprovalStore(reopened).allowed(
        session_id=session.session_id,
        command="git status --short",
        cwd="C:\\workspace",
        permission_target="workspace_write",
    )
    with sqlite3.connect(reopened.paths.session_db(session.session_id)) as connection:
        row = connection.execute(
            "SELECT command_hash,cwd_hash,command_summary,cwd_summary FROM sandbox_approvals"
        ).fetchone()
    assert row is not None
    persisted = " ".join(str(value) for value in row)
    assert "git status" not in persisted
    assert "C:\\workspace" not in persisted


class HistoryPlanner:
    name = "history-test"

    def __init__(self) -> None:
        self.histories: list[list[dict[str, str]]] = []

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        self.histories.append(list(history))
        return AgentAction(type="final_answer", answer=f"history has {len(history)} messages")


def test_sqlite_stores_keep_state_in_the_configured_session_root(tmp_path: Path) -> None:
    first = session_store(tmp_path / "first")
    second = session_store(tmp_path / "second")

    assert first.paths.root != second.paths.root


def test_session_store_reads_conversation_in_chronological_cursor_pages(tmp_path: Path) -> None:
    store = session_store(tmp_path / "store")
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


def test_manual_rename_locks_title_even_when_renamed_to_placeholder(tmp_path: Path) -> None:
    store = session_store(tmp_path / "store")
    session = store.create_session()
    store.start_turn(session.session_id, "run_one", "第一轮提问")
    store.finish_turn(session.session_id, "run_one", "completed", "ok")
    assert store.get_session(session.session_id).title == "第一轮提问"

    renamed = store.rename_session(session.session_id, "新对话")
    assert renamed.title_is_custom is True
    store.start_turn(session.session_id, "run_two", "第二轮提问")
    store.finish_turn(session.session_id, "run_two", "completed", "ok")
    reopened = session_store(tmp_path / "store")
    assert reopened.get_session(session.session_id).title == "新对话"
    assert reopened.get_session_summary(session.session_id).title_is_custom is True


def test_import_conversation_auto_titles_from_first_imported_user_message(tmp_path: Path) -> None:
    store = session_store(tmp_path / "store")
    session = store.import_conversation(
        None,
        [
            {"role": "assistant", "content": "welcome"},
            {"role": "user", "content": "  导入的  第一条消息 "},
        ],
    )
    assert session.title == "导入的 第一条消息"
    assert session.title_is_custom is False

    explicit = store.import_conversation("自定义导入", [{"role": "user", "content": "内容"}])
    assert explicit.title == "自定义导入"
    assert explicit.title_is_custom is True

    # A placeholder title never becomes custom; the first user message fills it.
    placeholder = store.import_conversation("新对话", [{"role": "user", "content": "正文"}])
    assert placeholder.title == "正文"
    assert placeholder.title_is_custom is False
