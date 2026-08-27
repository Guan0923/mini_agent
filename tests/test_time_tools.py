from pathlib import Path

import pytest

from backend.domain import DEFAULT_TIME_ZONE
from backend.planning import RuleBasedPlanner
from backend.runtime import AgentRunner, ConversationService, RuntimeState
from backend.tools import ToolError, ToolInvocationContext, ToolRegistry, build_tool_registry
from tests.local_store import session_store


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


def test_session_timezone_persists_in_runtime_snapshot(tmp_path: Path) -> None:
    store = session_store(tmp_path / "store")
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
