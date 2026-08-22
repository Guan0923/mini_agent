from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.chat import ChatRequest, ResumeRequest, _reasoning_parameters
from backend.api.chat.routes import _validate_source_node
from backend.api.interrupts import make_interactive_interrupt, registry
from backend.api.sessions.routes import _store
from backend.api.state import WebAppState
from backend.domain import NodeWriter, compaction_payload, message_payload
from backend.domain import RuntimeState as RuntimeNode
from backend.runtime.core.context import RuntimeState
from backend.runtime.core.contracts import InterruptRequest, QuestionOption, UserQuestion
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.storage.auth import LocalAuthStore
from backend.storage.sqlite import SQLiteSessionStore


def resolve_once(request: InterruptRequest, choice: str, **values):
    events: list[dict] = []
    handler = make_interactive_interrupt(events.append, timeout=2)
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(handler(request)))
    thread.start()
    deadline = time.monotonic() + 1
    while not events and time.monotonic() < deadline:
        time.sleep(0.01)
    assert events and events[0]["kind"] == "decision_requested"
    decision_id = events[0]["data"]["decision_id"]
    assert registry.resolve(decision_id, {"choice": choice, **values})
    thread.join(timeout=1)
    assert not thread.is_alive()
    return events[0], result[0]


def test_chat_request_accepts_mode_session_and_permission() -> None:
    request = ChatRequest(
        prompt="inspect",
        session_id="session_1",
        mode="plan",
        permission_mode="full_access",
        full_access_acknowledged=True,
    )

    assert request.mode == "plan"
    assert request.session_id == "session_1"
    assert request.permission_mode == "full_access"


def test_chat_and_resume_requests_validate_reasoning_effort() -> None:
    chat = ChatRequest(prompt="inspect", reasoning_effort="xhigh")
    resume = ResumeRequest(permission_mode="approval_for_me", reasoning_effort="low")

    assert chat.reasoning_effort == "xhigh"
    assert resume.reasoning_effort == "low"
    assert _reasoning_parameters(chat.reasoning_effort) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "xhigh",
    }


def test_interactive_decision_payload_includes_plan_and_question_options() -> None:
    request = InterruptRequest(
        "question",
        "Choose a direction",
        {"plan": "# Proposal", "details": "details"},
        (UserQuestion("q1", "Direction", "Which one?", (QuestionOption("A", "First"),)),),
    )

    event, decision = resolve_once(request, "answer", answers={"q1": ["A"]})

    question = event["data"]["questions"][0]
    assert question == {
        "id": "q1",
        "header": "Direction",
        "question": "Which one?",
        "options": [{"label": "A", "description": "First"}],
    }
    assert event["data"]["plan"] == "# Proposal"
    assert decision.choice == "answer"
    assert decision.answers == {"q1": ["A"]}


def test_interactive_decision_maps_plan_clear_resume_and_supplement() -> None:
    _, plan = resolve_once(InterruptRequest("plan", "review", {"plan": "# Plan"}), "implement_clear_session")
    _, resume = resolve_once(InterruptRequest("resume", "continue", {"details": "run"}), "back")
    _, tool = resolve_once(
        InterruptRequest("tool", "review", {"tool": "run_command"}), "supplement", supplement="use read-only"
    )

    assert plan.choice == "implement_clear_session"
    assert resume.choice == "back"
    assert tool.choice == "supplement"
    assert tool.supplement == "use read-only"


def test_interactive_tool_denial_does_not_map_to_run_cancellation() -> None:
    _, denied = resolve_once(InterruptRequest("tool", "review", {"tool": "write_file"}), "deny")
    _, allowed_once = resolve_once(InterruptRequest("tool", "review", {"tool": "write_file"}), "allow_once")
    _, allowed_session = resolve_once(InterruptRequest("tool", "review", {"tool": "write_file"}), "allow_session")
    stopped = make_interactive_interrupt(lambda _event: None, cancel_requested=lambda: True)(
        InterruptRequest("tool", "review", {"tool": "write_file"})
    )
    timed_out = make_interactive_interrupt(lambda _event: None, timeout=0.01)(
        InterruptRequest("tool", "review", {"tool": "write_file"})
    )

    assert denied.choice == "deny"
    assert allowed_once.choice == "continue"
    assert allowed_session.choice == "continue"
    assert stopped.choice == "cancel"
    assert timed_out.choice == "cancel"


def test_full_access_interrupt_auto_approves_tools_but_still_requests_plan() -> None:
    events: list[dict] = []
    handler = make_interactive_interrupt(events.append, timeout=1, auto_approve_tools=True)

    tool = handler(InterruptRequest("tool", "review", {"tool": "run_command"}))
    assert tool.choice == "continue"
    assert events == []


def test_web_app_registers_session_and_chat_routes(tmp_path: Path) -> None:
    auth = LocalAuthStore(tmp_path / "client.db")
    state = WebAppState(tmp_path / "web", auth_repository=auth)
    routes = set(create_app(state).openapi()["paths"])

    assert "/api/chat" in routes
    assert "/api/sessions" in routes
    assert "/api/sessions/{session_id}/compact" in routes
    assert "/api/sessions/{session_id}/trace" in routes
    assert "/api/forkable-runs" in routes
    assert "/api/ready" in routes


def _active_runtime_client(tmp_path: Path):
    state = WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))
    client = TestClient(create_app(state))
    identity = client.post("/api/auth/guest").json()["user"]
    session = client.post("/api/sessions", json={}).json()
    store = SQLiteSessionStore(state.user_paths(identity["id"]), f"web_{identity['id']}")
    store.start_turn(session["session_id"], "run-runtime-config", "hello")
    frames = []
    bridge = RuntimeEventNodeBridge(
        store,
        session_id=session["session_id"],
        prompt="hello",
        user=identity["id"],
        provider="chat_completions",
        provider_name="default",
        model="demo-chat",
        emit=frames.append,
    )
    bridge.start()
    state.active_runtime_bridges[(identity["id"], session["session_id"])] = bridge
    return state, client, identity, session, store, bridge


def test_runtime_config_patch_requires_a_live_dynamic_leaf(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))
    with TestClient(create_app(state)) as client:
        client.post("/api/auth/guest")
        session = client.post("/api/sessions", json={}).json()
        response = client.patch(
            f"/api/sessions/{session['session_id']}/runtime-config",
            json={"node_id": "node_missing", "permission_mode": "full_access", "full_access_acknowledged": True},
        )
    assert response.status_code == 409


def test_runtime_config_patch_validates_atomically_and_switches_provider_defaults(tmp_path: Path) -> None:
    state, client, identity, session, store, bridge = _active_runtime_client(tmp_path)
    try:
        state.settings.add_provider_config(
            identity["id"],
            {
                "provider_name": "work-openai",
                "protocol": "chat_completions",
                "base_url": "https://example.test/v1",
                "model": "provider-default",
                "max_tokens": 16000,
                "context_size": 128000,
            },
        )
        node_id = bridge.assistant.id
        response = client.patch(
            f"/api/sessions/{session['session_id']}/runtime-config",
            json={
                "node_id": node_id,
                "provider_name": "WORK-OPENAI",
                "model": {"reasoning_effort": "high"},
                "permission_mode": "full_access",
                "full_access_acknowledged": True,
            },
        )
        assert response.status_code == 200, response.text
        dynamic = bridge.writer.current(session["session_id"], node_id)
        assert dynamic.provider_name == "work-openai"
        assert dynamic.model["current_model"] == "provider-default"
        assert dynamic.model["output_length"] == 16000
        assert dynamic.model["reasoning_effort"] == "high"
        assert dynamic.permission_mode == "full_access"

        # A later partial update must use the live dynamic node as its base;
        # it must not silently restore provider defaults for omitted fields.
        preserved = client.patch(
            f"/api/sessions/{session['session_id']}/runtime-config",
            json={"node_id": node_id, "permission_mode": "approval_for_me"},
        )
        assert preserved.status_code == 200, preserved.text
        dynamic = bridge.writer.current(session["session_id"], node_id)
        assert dynamic.model["output_length"] == 16000
        assert dynamic.model["reasoning_effort"] == "high"
        assert dynamic.permission_mode == "read_only"

        before = dynamic.to_dict()
        invalid = client.patch(
            f"/api/sessions/{session['session_id']}/runtime-config",
            json={"node_id": node_id, "model": {"context_length": 1}},
        )
        assert invalid.status_code == 422
        assert bridge.writer.current(session["session_id"], node_id).to_dict() == before
    finally:
        client.close()


def test_runtime_config_patch_rejects_a_sealed_node_even_when_it_is_a_leaf(tmp_path: Path) -> None:
    state, client, identity, session, store, bridge = _active_runtime_client(tmp_path)
    try:
        user_node = next(
            node
            for node in store.load_nodes(session["session_id"])
            if node.data_type == "message" and node.role == "user"
        )
        response = client.patch(
            f"/api/sessions/{session['session_id']}/runtime-config",
            json={"node_id": user_node.id, "permission_mode": "full_access", "full_access_acknowledged": True},
        )
        assert response.status_code == 409
    finally:
        client.close()


def test_project_name_and_path_management_are_validated(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with TestClient(create_app(state)) as client:
        user = client.post("/api/auth/guest").json()["user"]
        project = state.projects(user["id"]).create(first)

        renamed = client.patch(f"/api/projects/{project.project_id}", json={"name": "  我的项目  "})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "我的项目"
        assert client.patch(f"/api/projects/{project.project_id}", json={"name": " "}).status_code == 422

        session_store = _store(state, user["id"])
        session = session_store.create_session("已有历史", local_only=True)
        state.projects(user["id"]).create_session(project.project_id, session.session_id)
        session_store.save_runtime(RuntimeState(session_id=session.session_id))

        state.project_picker = lambda: second
        changed = client.post(f"/api/projects/{project.project_id}/path")
        assert changed.status_code == 200, changed.text
        assert changed.json()["cwd"] == str(second.resolve())
        assert changed.json()["name"] == "我的项目"

        state.project_picker = lambda: None
        assert client.post(f"/api/projects/{project.project_id}/path").status_code == 204
        assert client.post(f"/api/projects/{project.project_id}/remove").status_code == 200
        assert client.patch(f"/api/projects/{project.project_id}", json={"name": "之后"}).status_code == 400


def test_project_path_update_allows_duplicate_active_cwd(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with TestClient(create_app(state)) as client:
        user = client.post("/api/auth/guest").json()["user"]
        state.projects(user["id"]).create(first)
        other = state.projects(user["id"]).create(second)
        state.project_picker = lambda: first
        response = client.post(f"/api/projects/{other.project_id}/path")
        assert response.status_code == 200, response.text
        assert response.json()["cwd"] == str(first.resolve())


def test_project_skill_trust_exposes_and_revokes_trust(tmp_path: Path) -> None:
    from backend.configuration import UserConfigStore
    from backend.skills.trust import ProjectSkillTrustStore

    state = WebAppState(tmp_path / "web")
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    with TestClient(create_app(state)) as client:
        user = client.post("/api/auth/guest").json()["user"]
        created = state.projects(user["id"]).create(project_dir)

        # No trust recorded yet.
        details = client.get(f"/api/projects/{created.project_id}/skill-trust")
        assert details.status_code == 200, details.text
        assert details.json()["trusted_skills"] == {}

        # Record trust directly in the user config.
        paths = state.user_paths(user["id"])
        store = ProjectSkillTrustStore(UserConfigStore(paths.config_file))
        workspace_sha = details.json()["workspace_sha256"]
        store.record_trust(created.project_id, workspace_sha, "demo", "a" * 64)

        details = client.get(f"/api/projects/{created.project_id}/skill-trust")
        assert details.status_code == 200
        assert details.json()["trusted_skills"] == {"demo": {"tree_sha256": "a" * 64}}

        revoked = client.delete(f"/api/projects/{created.project_id}/skill-trust")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["trusted_skills"] == {}
        assert store.is_trusted(created.project_id, workspace_sha, "demo", "a" * 64) is False

        missing = client.get("/api/projects/not-a-project/skill-trust")
        assert missing.status_code == 404


def test_web_default_session_title_is_renamed_on_first_turn(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        user = client.post("/api/auth/guest").json()["user"]
        store = _store(state, user["id"])
        session = store.create_session("新对话", local_only=True)

        store.start_turn(session.session_id, "run_first", "检查项目中的测试失败")

        summary = store.get_session_summary(session.session_id)
        assert summary is not None
        assert summary.title == "检查项目中的测试失败"


def test_session_summary_counts_user_and_assistant_messages_not_runtime_nodes(
    tmp_path: Path,
) -> None:
    state = WebAppState(tmp_path / "web")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with TestClient(create_app(state)) as client:
        user = client.post("/api/auth/guest").json()["user"]
        store = _store(state, user["id"])
        project = state.projects(user["id"]).create(project_dir)
        session = store.create_session("两轮交互", local_only=True)
        state.projects(user["id"]).create_session(project.project_id, session.session_id)

        writer = NodeWriter(store)
        parent: RuntimeNode | None = None
        for turn in range(2):
            user_node = writer.create(
                session_id=session.session_id,
                parent=parent,
                data=message_payload("user", f"用户消息 {turn}"),
            )
            parent = writer.delete(user_node.session_id, user_node.id)
            assistant_node = writer.create(
                session_id=session.session_id,
                parent=parent,
                data=message_payload("assistant", f"回答 {turn}"),
            )
            parent = writer.delete(assistant_node.session_id, assistant_node.id)
            tool_node = writer.create(
                session_id=session.session_id,
                parent=parent,
                data=message_payload("tool_result", f"工具结果 {turn}"),
            )
            parent = writer.delete(tool_node.session_id, tool_node.id)
            follow_up_node = writer.create(
                session_id=session.session_id,
                parent=parent,
                data=message_payload("assistant", f"补充回答 {turn}"),
            )
            parent = writer.delete(follow_up_node.session_id, follow_up_node.id)
            compaction_node = writer.create(
                session_id=session.session_id,
                parent=parent,
                data=compaction_payload(f"摘要 {turn}"),
            )
            parent = writer.delete(compaction_node.session_id, compaction_node.id)

        summary = store.get_session_summary(session.session_id)
        assert summary is not None
        assert len(store.load_nodes(session.session_id)) == 11
        assert summary.message_count == 6

        empty = store.create_session("空会话")
        empty_summary = store.get_session_summary(empty.session_id)
        assert empty_summary is not None
        assert empty_summary.message_count == 0


def test_rewind_inherits_title_provenance_and_retitles_without_first_user(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        user = client.post("/api/auth/guest").json()["user"]
        store = _store(state, user["id"])

        def source_session() -> tuple[str, str]:
            session = store.create_session("新对话")
            store.start_turn(session.session_id, "run-first", "检查项目中的测试失败")
            store.finish_turn(session.session_id, "run-first", "completed", "好的")
            writer = NodeWriter(store)
            parent: RuntimeNode | None = None
            first_user = None
            for content in ("检查项目中的测试失败", "已修复"):
                user_node = writer.create(
                    session_id=session.session_id,
                    parent=parent,
                    data=message_payload("user", content),
                )
                first_user = user_node if first_user is None else first_user
                parent = writer.delete(user_node.session_id, user_node.id)
                assistant_node = writer.create(
                    session_id=session.session_id,
                    parent=parent,
                    data=message_payload("assistant", "好的"),
                )
                parent = writer.delete(assistant_node.session_id, assistant_node.id)
            assert store.get_session(session.session_id).title == "检查项目中的测试失败"
            assert store.get_session(session.session_id).title_is_custom is False
            return session.session_id, first_user.id

        # Rewind keeping the first user message: the automatic title and its
        # provenance are inherited, and the next prompt cannot replace it.
        source_id, first_user_id = source_session()
        kept = client.post(
            f"/api/sessions/{source_id}/rewind",
            json={
                "title": "检查项目中的测试失败",
                "client_id": "rewound-keep",
                "source_node_id": first_user_id,
            },
        )
        assert kept.status_code == 200, kept.text
        kept_payload = kept.json()
        assert kept_payload["title"] == "检查项目中的测试失败"
        assert kept_payload["title_is_custom"] is False
        store.start_turn(kept_payload["session_id"], "run-kept", "后续问题")
        assert store.get_session_summary(kept_payload["session_id"]).title == "检查项目中的测试失败"

        # Rewind past the first user message (empty fallback history): the
        # next prompt becomes the new first user and replaces the auto title.
        source_id, _first_user_id = source_session()
        past = client.post(
            f"/api/sessions/{source_id}/rewind",
            json={"title": "检查项目中的测试失败", "client_id": "rewound-past", "fallback_messages": []},
        )
        assert past.status_code == 200, past.text
        past_payload = past.json()
        assert past_payload["title_is_custom"] is False
        store.start_turn(past_payload["session_id"], "run-past", "全新开始")
        assert store.get_session_summary(past_payload["session_id"]).title == "全新开始"

        # A manual title survives both rewind shapes.
        source_id, first_user_id = source_session()
        store.rename_session(source_id, "手工标题")
        assert store.get_session(source_id).title_is_custom is True
        manual = client.post(
            f"/api/sessions/{source_id}/rewind",
            json={"title": "手工标题", "client_id": "rewound-manual", "source_node_id": first_user_id},
        )
        assert manual.status_code == 200, manual.text
        manual_payload = manual.json()
        assert manual_payload["title"] == "手工标题"
        assert manual_payload["title_is_custom"] is True
        store.start_turn(manual_payload["session_id"], "run-manual", "提问")
        assert store.get_session_summary(manual_payload["session_id"]).title == "手工标题"


@pytest.mark.parametrize("source_status", ["running", "abort", "success"])
def test_fork_and_rewind_use_resumable_anchor_for_terminal_attempts(
    tmp_path: Path,
    source_status: str,
) -> None:
    state = WebAppState(tmp_path / source_status)
    with TestClient(create_app(state)) as client:
        user = client.post("/api/auth/guest").json()["user"]
        store = _store(state, user["id"])
        source = store.create_session(f"source-{source_status}")
        root = store.get_session_root(source.session_id)
        assert root is not None
        writer = NodeWriter(store)
        stable = writer.create(
            session_id=source.session_id,
            parent=root,
            data=message_payload("assistant", "stable context"),
        )
        stable = writer.delete(stable.session_id, stable.id)
        selected = writer.create(
            session_id=source.session_id,
            parent=stable,
            data=message_payload("assistant", "selected attempt"),
        )
        if source_status == "abort":
            selected = writer.abort(selected.session_id, selected.id)
        elif source_status == "success":
            selected = writer.delete(selected.session_id, selected.id)

        before_rewind = {node.key for node in store.load_nodes(source.session_id)}
        rewound = client.post(
            f"/api/sessions/{source.session_id}/rewind",
            json={
                "source_node_id": selected.id,
                "source_node_session_id": selected.session_id,
            },
        )
        assert rewound.status_code == 200, rewound.text
        assert rewound.json()["rewind_source_node_id"] == stable.id
        assert rewound.json()["rewind_source_session_id"] == stable.session_id
        assert {node.key for node in store.load_nodes(source.session_id)} == before_rewind

        forked = client.post(
            f"/api/sessions/{source.session_id}/fork",
            json={
                "title": f"fork-{source_status}",
                "source_node_id": selected.id,
                "source_node_session_id": selected.session_id,
            },
        )
        assert forked.status_code == 200, forked.text
        target_root = store.get_session_root(forked.json()["session_id"])
        assert target_root is not None
        expected_anchor = selected if source_status == "success" else stable
        assert (target_root.parent_session_id, target_root.parent_id) == expected_anchor.key
        assert forked.json()["fork_anchor_node_id"] == expected_anchor.id
        assert forked.json()["fork_anchor_session_id"] == expected_anchor.session_id
        if source_status in {"running", "abort"}:
            assert store.list_children(selected.session_id, selected.id) == []
            persisted = store.get_node(selected.session_id, selected.id)
            assert persisted is not None and persisted.status == source_status


def test_fork_accepts_only_full_node_keys_from_the_loaded_ancestor_tree(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        user = client.post("/api/auth/guest").json()["user"]
        store = _store(state, user["id"])
        writer = NodeWriter(store)

        ancestor_session = store.create_session("ancestor")
        ancestor_root = store.get_session_root(ancestor_session.session_id)
        assert ancestor_root is not None
        ancestor = writer.create(
            session_id=ancestor_session.session_id,
            parent=ancestor_root,
            data=message_payload("assistant", "ancestor"),
        )
        ancestor = writer.delete(ancestor.session_id, ancestor.id)
        branch = store.create_session("branch", root_parent=ancestor.key)

        valid = client.post(
            f"/api/sessions/{branch.session_id}/fork",
            json={"source_node_id": ancestor.id, "source_node_session_id": ancestor.session_id},
        )
        assert valid.status_code == 200, valid.text
        valid_root = store.get_session_root(valid.json()["session_id"])
        assert valid_root is not None
        assert (valid_root.parent_session_id, valid_root.parent_id) == ancestor.key
        assert valid.json()["fork_anchor_node_id"] == ancestor.id
        assert valid.json()["fork_anchor_session_id"] == ancestor.session_id

        unrelated_session = store.create_session("unrelated")
        unrelated_root = store.get_session_root(unrelated_session.session_id)
        assert unrelated_root is not None
        unrelated = writer.create(
            session_id=unrelated_session.session_id,
            parent=unrelated_root,
            data=message_payload("assistant", "unrelated"),
        )
        unrelated = writer.delete(unrelated.session_id, unrelated.id)
        rejected = client.post(
            f"/api/sessions/{branch.session_id}/fork",
            json={"source_node_id": unrelated.id, "source_node_session_id": unrelated.session_id},
        )
        assert rejected.status_code == 400


def test_source_node_validation_allows_non_leaf_ancestor_and_preserves_boundaries(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        user = client.post("/api/auth/guest").json()["user"]
        store = _store(state, user["id"])
        writer = NodeWriter(store)

        source_session = store.create_session("source")
        source_root = store.get_session_root(source_session.session_id)
        assert source_root is not None
        source = writer.create(
            session_id=source_session.session_id,
            parent=source_root,
            data=message_payload("assistant", "fork anchor"),
        )
        source = writer.delete(source.session_id, source.id)
        existing_child = writer.create(
            session_id=source_session.session_id,
            parent=source,
            data=message_payload("user", "existing branch"),
        )
        writer.delete(existing_child.session_id, existing_child.id)
        branch = store.create_session("branch", root_parent=source.key)

        _validate_source_node(
            store,
            branch.session_id,
            source.id,
            source_node_session_id=source.session_id,
        )

        unrelated_session = store.create_session("unrelated")
        unrelated_root = store.get_session_root(unrelated_session.session_id)
        assert unrelated_root is not None
        unrelated = writer.create(
            session_id=unrelated_session.session_id,
            parent=unrelated_root,
            data=message_payload("assistant", "unrelated"),
        )
        unrelated = writer.delete(unrelated.session_id, unrelated.id)
        with pytest.raises(HTTPException, match="source_node_id 不属于当前会话祖先树"):
            _validate_source_node(
                store,
                branch.session_id,
                unrelated.id,
                source_node_session_id=unrelated.session_id,
            )

        with pytest.raises(HTTPException, match="只能从 running、cancel 或 abort 节点恢复"):
            _validate_source_node(
                store,
                branch.session_id,
                source.id,
                source_node_session_id=source.session_id,
                resume=True,
            )


def test_chat_request_accepts_and_limits_structured_references() -> None:
    from backend.api.chat.routes import ChatRequest, FileReference

    request = ChatRequest(
        prompt="查看引用文件",
        session_id="session_1",
        references=[FileReference(source="upload", path="notes.md")],
    )
    assert request.references[0].source == "upload"
    assert request.references[0].path == "notes.md"

    with pytest.raises(Exception):
        ChatRequest(prompt="x", references=[{"source": "other", "path": "x"}])


def test_references_persist_on_user_node_and_expander_is_skipped(tmp_path: Path) -> None:
    from backend.runtime.node_bridge import RuntimeEventNodeBridge
    from backend.storage.sqlite import SQLiteSessionStore

    state = WebAppState(tmp_path / "web")
    client = TestClient(create_app(state))
    user = client.post("/api/auth/guest").json()["user"]
    store = SQLiteSessionStore(state.user_paths(user["id"]), f"web_{user['id']}")
    session = store.create_session("引用测试")
    frames: list[object] = []
    bridge = RuntimeEventNodeBridge(
        store,
        session_id=session.session_id,
        prompt="请分析 @notes.md 的内容",
        user=user["id"],
        provider="chat_completions",
        provider_name="default",
        model="demo-chat",
        references=[{"source": "upload", "path": "notes.md"}],
        emit=frames.append,
    )
    bridge.start()
    nodes = store.load_nodes(session.session_id)
    user_nodes = [node for node in nodes if node.role == "user"]
    assert user_nodes, "bridge must persist a user node"
    assert user_nodes[-1].message.get("references") == [{"source": "upload", "path": "notes.md"}]


def test_structured_references_do_not_expand_file_contents() -> None:
    from backend.runtime.conversation.references import FileReferenceExpander

    expander = FileReferenceExpander(_ReadOnlyFiles())
    task = "请查看 @readme.md 的说明"
    assert expander.expand(task, structured=True) == task


class _ReadOnlyFiles:
    """Minimal files double proving the expander never reads with structured refs."""

    def read_text(self, _path: str) -> str:
        raise AssertionError("structured references must not read file contents")
