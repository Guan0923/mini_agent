import sqlite3
from pathlib import Path

from backend.domain import AgentAction, NodeWriter, RunState, message_payload
from backend.runtime import AgentRunner
from backend.sandbox import ApprovalStore
from backend.tools import ToolRegistry
from tests.local_store import session_store
from tui.cli import TerminalApp


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
        columns = {row[1] for row in connection.execute("PRAGMA table_info(session_runs)").fetchall()}
        lineage = connection.execute(
            "SELECT workflow_id, attempt, origin_kind FROM session_runs WHERE run_id = ?",
            ("run_schema",),
        ).fetchone()
    assert {"workflow_id", "attempt", "origin_kind", "source_session_id", "source_run_id"} <= columns
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


def test_v6_permission_migration_downgrades_legacy_full_access(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = session_store(root)
    session = store.create_session("Legacy permission")
    with sqlite3.connect(store.paths.session_db(session.session_id)) as connection:
        connection.execute("UPDATE session_meta SET schema_version=6")
        connection.execute("UPDATE runtime_nodes SET permission_mode='full_access'")

    reopened = session_store(root)
    root_node = reopened.get_session_root(session.session_id)
    assert root_node is not None
    assert root_node.permission_mode == "read_only"


class HistoryPlanner:
    name = "history-test"

    def __init__(self) -> None:
        self.histories: list[list[dict[str, str]]] = []

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


def test_first_turn_auto_title_uses_first_user_message_and_survives_refresh(tmp_path: Path) -> None:
    """The auto title equals the first user message and never regresses.

    This mirrors the Web ordering: ``start_turn`` persists the run metadata
    and the automatic title before the node bridge commits the first user
    message.  Later turns and reopened stores keep that first-message title.
    """

    store = session_store(tmp_path / "store")
    session = store.create_session("新对话")
    store.start_turn(session.session_id, "run_one", "  检查  项目中的测试失败  ")
    writer = NodeWriter(store)
    root = store.get_session_root(session.session_id)
    assert root is not None
    user = writer.create(
        session_id=session.session_id,
        parent=root,
        data=message_payload("user", "  检查  项目中的测试失败  "),
    )
    writer.delete(user.session_id, user.id)
    store.finish_turn(session.session_id, "run_one", "completed", "done")

    summary = store.get_session_summary(session.session_id)
    assert summary is not None
    assert summary.title == "检查 项目中的测试失败"
    assert summary.title_is_custom is False

    store.start_turn(session.session_id, "run_two", "继续")
    store.finish_turn(session.session_id, "run_two", "completed", "ok")
    reopened = session_store(tmp_path / "store")
    assert reopened.get_session_summary(session.session_id).title == "检查 项目中的测试失败"
    assert reopened.get_session_summary(session.session_id).title_is_custom is False


def test_user_node_committed_before_start_turn_is_never_retitled(tmp_path: Path) -> None:
    """Regression: an already-committed first user message blocks auto naming.

    The pre-fix Web flow wrote the first user node before ``start_turn``.
    The current prompt is then not the first user message, so the placeholder
    title must be preserved instead of being overwritten.
    """

    store = session_store(tmp_path / "store")
    session = store.create_session("新对话")
    writer = NodeWriter(store)
    root = store.get_session_root(session.session_id)
    assert root is not None
    user = writer.create(session_id=session.session_id, parent=root, data=message_payload("user", "旧消息"))
    writer.delete(user.session_id, user.id)
    store.start_turn(session.session_id, "run_one", "新任务")
    assert store.get_session(session.session_id).title == "新对话"
    assert store.get_session(session.session_id).title_is_custom is False


def test_failed_first_turn_keeps_auto_title_once_user_message_is_committed(tmp_path: Path) -> None:
    store = session_store(tmp_path / "store")
    session = store.create_session("新对话")
    store.start_turn(session.session_id, "run_failed", "第一次提问")
    writer = NodeWriter(store)
    root = store.get_session_root(session.session_id)
    assert root is not None
    user = writer.create(session_id=session.session_id, parent=root, data=message_payload("user", "第一次提问"))
    writer.delete(user.session_id, user.id)
    store.finish_turn(session.session_id, "run_failed", "failed", None)

    assert store.get_session_summary(session.session_id).title == "第一次提问"
    assert store.get_session(session.session_id).title_is_custom is False


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


def test_fork_title_is_locked_custom_and_rewind_inherits_provenance(tmp_path: Path) -> None:
    store = session_store(tmp_path / "store")
    source = store.create_session("新对话")
    store.start_turn(source.session_id, "run_one", "第一条用户")
    store.finish_turn(source.session_id, "run_one", "completed", "好的")
    writer = NodeWriter(store)
    root = store.get_session_root(source.session_id)
    assert root is not None
    first_user = writer.create(session_id=source.session_id, parent=root, data=message_payload("user", "第一条用户"))
    first_user = writer.delete(first_user.session_id, first_user.id)
    second_user = writer.create(
        session_id=source.session_id,
        parent=first_user,
        data=message_payload("user", "第二条用户"),
    )
    second_user = writer.delete(second_user.session_id, second_user.id)
    assistant = writer.create(
        session_id=source.session_id,
        parent=second_user,
        data=message_payload("assistant", "回答"),
    )
    writer.delete(assistant.session_id, assistant.id)
    assert store.get_session(source.session_id).title == "第一条用户"
    source = store.get_session(source.session_id)
    assert source is not None

    # A fork title (derived from the source title) is locked as custom.
    fork = store.create_session(f"{source.title}（分支）", root_parent=(source.session_id, second_user.id))
    assert fork.title_is_custom is True
    store.start_turn(fork.session_id, "run_fork", "分支提问")
    assert store.get_session(fork.session_id).title == "第一条用户（分支）"

    # Rewind keeping the first user: automatic provenance is inherited and the
    # next prompt cannot replace the first-message title.
    rewind_keep = store.create_session(
        source.title,
        root_parent=(source.session_id, second_user.id),
        title_is_custom=False,
    )
    store.start_turn(rewind_keep.session_id, "run_keep", "后续问题")
    assert store.get_session(rewind_keep.session_id).title == "第一条用户"
    assert store.get_session(rewind_keep.session_id).title_is_custom is False

    # Rewind past the first user: the next prompt becomes the new first user.
    rewind_past = store.import_conversation(source.title, [], force_new=True, title_is_custom=False)
    store.start_turn(rewind_past.session_id, "run_past", "全新开始")
    assert store.get_session(rewind_past.session_id).title == "全新开始"
    assert store.get_session(rewind_past.session_id).title_is_custom is False

    # A manual title survives both kinds of rewind.
    store.rename_session(source.session_id, "手工标题")
    manual_keep = store.create_session(
        "手工标题",
        root_parent=(source.session_id, second_user.id),
        title_is_custom=True,
    )
    store.start_turn(manual_keep.session_id, "run_mk", "提问")
    assert store.get_session(manual_keep.session_id).title == "手工标题"
    manual_past = store.import_conversation("手工标题", [], force_new=True, title_is_custom=True)
    store.start_turn(manual_past.session_id, "run_mp", "提问")
    assert store.get_session(manual_past.session_id).title == "手工标题"


def test_v5_database_migration_backfills_title_provenance(tmp_path: Path) -> None:
    # Placeholder title with a user message: backfill the first local user
    # message and keep automatic naming.
    first = session_store(tmp_path / "one")
    session = first.create_session("新对话")
    writer = NodeWriter(first)
    root = first.get_session_root(session.session_id)
    assert root is not None
    user = writer.create(session_id=session.session_id, parent=root, data=message_payload("user", "  旧版  消息 "))
    writer.delete(user.session_id, user.id)
    with sqlite3.connect(first.paths.session_db(session.session_id)) as connection:
        connection.execute("UPDATE session_meta SET schema_version=5")
        connection.execute("ALTER TABLE session_meta DROP COLUMN title_is_custom")
    migrated = session_store(tmp_path / "one")
    meta = migrated.get_session(session.session_id)
    assert meta.title == "旧版 消息"
    assert meta.title_is_custom is False

    # Non-placeholder title: conservatively treated as a manual rename.
    second = session_store(tmp_path / "two")
    session_two = second.create_session("历史标题")
    with sqlite3.connect(second.paths.session_db(session_two.session_id)) as connection:
        connection.execute("UPDATE session_meta SET schema_version=5")
        connection.execute("ALTER TABLE session_meta DROP COLUMN title_is_custom")
    migrated_two = session_store(tmp_path / "two")
    assert migrated_two.get_session(session_two.session_id).title == "历史标题"
    assert migrated_two.get_session(session_two.session_id).title_is_custom is True

    # No user message: keep the placeholder title and automatic naming.
    third = session_store(tmp_path / "three")
    session_three = third.create_session()
    with sqlite3.connect(third.paths.session_db(session_three.session_id)) as connection:
        connection.execute("UPDATE session_meta SET schema_version=5")
        connection.execute("ALTER TABLE session_meta DROP COLUMN title_is_custom")
    migrated_three = session_store(tmp_path / "three")
    meta_three = migrated_three.get_session(session_three.session_id)
    assert meta_three.title == "New session"
    assert meta_three.title_is_custom is False

    # Legacy projection fallback: pre-node stores backfill from session_messages.
    legacy = session_store(tmp_path / "four")
    session_four = legacy.create_session("New session")
    with sqlite3.connect(legacy.paths.session_db(session_four.session_id)) as connection:
        connection.execute(
            "INSERT INTO session_messages(run_id, role, content, created_at) "
            "VALUES ('run_legacy', 'user', '  遗留  消息 ', '2026-01-01T00:00:00+00:00')"
        )
        connection.execute("UPDATE session_meta SET schema_version=5")
        connection.execute("ALTER TABLE session_meta DROP COLUMN title_is_custom")
    migrated_four = session_store(tmp_path / "four")
    assert migrated_four.get_session(session_four.session_id).title == "遗留 消息"
    assert migrated_four.get_session(session_four.session_id).title_is_custom is False
