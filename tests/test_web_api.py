from __future__ import annotations

import threading
import time
from pathlib import Path

from backend.api.app import create_app
from backend.api.chat import ChatRequest, ResumeRequest, _reasoning_parameters
from backend.api.interrupts import make_interactive_interrupt, registry
from backend.api.state import WebAppState
from backend.runtime.core.contracts import InterruptRequest, QuestionOption, UserQuestion
from backend.storage.auth import AuthStore


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
    auth = AuthStore(tmp_path / "auth.sqlite3")
    state = WebAppState(tmp_path / "web", auth_repository=auth, settings_repository=auth)
    routes = set(create_app(state).openapi()["paths"])

    assert "/api/chat" in routes
    assert "/api/sessions" in routes
    assert "/api/sessions/{session_id}/compact" in routes
    assert "/api/sessions/{session_id}/trace" in routes
    assert "/api/forkable-runs" in routes
    assert "/api/ready" in routes
