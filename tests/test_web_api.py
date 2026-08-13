from __future__ import annotations

import threading
import time
from pathlib import Path

from backend.api.app import create_app
from backend.api.chat import ChatRequest, ResumeRequest, _reasoning_parameters
from backend.api.interrupts import make_interactive_interrupt, registry
from backend.api.state import WebAppState
from backend.runtime.core.contracts import InterruptRequest, QuestionOption, UserQuestion
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.storage.auth import LocalAuthStore
from backend.storage.sqlite import SQLiteSessionStore
from fastapi.testclient import TestClient


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
        provider="deepseek",
        provider_name="deepseek",
        model="deepseek-chat",
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
            json={"node_id": "node_missing", "permission_mode": "full_access"},
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
        assert dynamic.permission_mode == "approval_for_me"

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
        user_node = store.load_nodes(session["session_id"])[0]
        response = client.patch(
            f"/api/sessions/{session['session_id']}/runtime-config",
            json={"node_id": user_node.id, "permission_mode": "full_access"},
        )
        assert response.status_code == 409
    finally:
        client.close()
