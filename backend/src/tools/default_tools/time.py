"""Read-only current-time tool backed by the runtime clock."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..base import Tool, ToolError, ToolInvocationContext
from .schema import object_schema


def time_tools() -> tuple[Tool, ...]:
    """Build the session-aware current-time tool."""

    return (
        Tool(
            "get_current_time",
            (
                "Returns the current date and time in the session-selected time zone. "
                "Call this tool whenever a task depends on the real current time, date, or time zone."
            ),
            _default_current_time,
            object_schema({}),
            context_handler=_get_current_time,
        ),
    )


def _default_current_time() -> str:
    return _get_current_time(ToolInvocationContext())


def _get_current_time(context: ToolInvocationContext) -> str:
    try:
        raw_time = context.clock()
        observed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ToolError("Runtime clock must return an ISO 8601 timestamp.") from exc

    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    else:
        observed = observed.astimezone(UTC)
    try:
        local_time = observed.astimezone(ZoneInfo(context.timezone))
    except ZoneInfoNotFoundError as exc:
        raise ToolError(f"Unsupported time zone: {context.timezone}") from exc

    offset = local_time.strftime("%z")
    formatted_offset = f"{offset[:3]}:{offset[3:]}"
    return f"Current time: {local_time.isoformat()}\nTime zone: {context.timezone}\nUTC offset: {formatted_offset}"
