from pathlib import Path
from types import SimpleNamespace

import pytest
from backend.domain import DEFAULT_TIME_ZONE
from backend.planning import RuleBasedPlanner
from backend.planning.prompts import compose_system_prompt
from backend.runtime import AgentRunner, ConversationService, RuntimeState
from backend.storage.postgres import PostgresSessionStore
from backend.tools import ToolError, ToolInvocationContext, ToolRegistry, build_tool_registry
from tui.application.commands import CommandAppMixin
from tui.components.commands import render_help
from tui.components.completion import SlashCommandCompleter


def test_current_time_tool_uses_runtime_context_and_validates_schema(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)
    context = ToolInvocationContext(
        session_id="session_1",
        timezone="America/New_York",
        clock=lambda: "2026-07-26T12:34:56+00:00",
    )

    output = registry.invoke_with_context("get_current_time", {}, context)

    assert "2026-07-26T08:34:56-04:00" in output
    assert "Time zone: America/New_York" in output
    assert "UTC offset: -04:00" in output
    assert registry.is_read_only("get_current_time") is True
    with pytest.raises(ToolError, match="Additional properties"):
        registry.invoke("get_current_time", {"timezone": "UTC"})


def test_current_time_tool_defaults_and_rejects_invalid_timezone(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    assert "Time zone: Asia/Shanghai" in registry.invoke("get_current_time", {})
    with pytest.raises(ToolError, match="Unsupported time zone"):
        registry.invoke_with_context("get_current_time", {}, ToolInvocationContext(timezone="Mars/Olympus"))


def test_runtime_timezone_round_trip_and_legacy_default() -> None:
    state = RuntimeState(session_id="session_1", timezone="Europe/London")

    assert RuntimeState.from_dict(state.to_dict()).timezone == "Europe/London"
    assert RuntimeState.from_dict({"session_id": "legacy"}).timezone == DEFAULT_TIME_ZONE


def test_session_timezone_persists_in_runtime_snapshot() -> None:
    store = PostgresSessionStore()
    service = ConversationService(AgentRunner(RuleBasedPlanner(), ToolRegistry()), store)

    assert service.current_timezone == DEFAULT_TIME_ZONE
    assert service.set_timezone("Asia/Tokyo") == "Asia/Tokyo"
    assert service.active_session is not None

    reopened = ConversationService(
        AgentRunner(RuleBasedPlanner(), ToolRegistry()),
        store,
        session_id=service.active_session.session_id,
    )

    assert reopened.current_timezone == "Asia/Tokyo"


class _Conversation:
    def __init__(self) -> None:
        self.current_timezone = DEFAULT_TIME_ZONE
        self.active_session = None
        self.selections: list[str] = []

    def set_timezone(self, timezone: str) -> str:
        self.selections.append(timezone)
        self.current_timezone = timezone
        self.active_session = SimpleNamespace(session_id="session_time")
        return timezone


class _View:
    def __init__(self) -> None:
        self.reviews: list[dict[str, object]] = []

    def begin_review(self, *args, **kwargs) -> None:
        self.reviews.append({"args": args, "kwargs": kwargs})


class _CommandApp(CommandAppMixin):
    def __init__(self) -> None:
        self._conversation_service = _Conversation()
        self._view = _View()
        self.messages: list[str] = []

    @property
    def active_session(self):
        return self._conversation_service.active_session

    def _write(self, text: str, end: str = "\n") -> None:
        self.messages.append(text)

    def _print_active_session(self) -> None:
        self.messages.append("SESSION CREATED")


def test_time_command_selects_and_persists_a_menu_timezone() -> None:
    app = _CommandApp()

    assert app._split_input("/time Asia/Tokyo") == [("command", "time", "Asia/Tokyo")]
    assert "/time" in [item.value for item in SlashCommandCompleter().suggestions("/t", 2)]
    assert "/time" in render_help()
    assert app._handle_command("time", "") is True
    review = app._view.reviews[0]
    assert [item.id for item in review["args"][3]][:2] == ["UTC", "Asia/Shanghai"]
    callback = review["args"][4]
    callback("Asia/Tokyo", None)
    callback("cancel", None)
    assert app._conversation_service.selections == ["Asia/Tokyo"]
    assert app.messages[-1] == "Time zone set to Asia/Tokyo."


def test_time_command_reports_noninteractive_selector_requirement() -> None:
    app = _CommandApp()
    app._view = None

    assert app._handle_command("time", "") is True
    assert app.messages == ["Time zone selector requires the interactive TUI."]


def test_system_prompt_requires_current_time_tool() -> None:
    assert "call `get_current_time`" in compose_system_prompt("agent")
