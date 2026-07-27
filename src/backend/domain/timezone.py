"""Session-scoped time-zone policy shared by the runtime and TUI."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TIME_ZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class TimeZoneOption:
    """One time zone exposed by the interactive session selector."""

    identifier: str
    label: str


TIME_ZONE_OPTIONS = (
    TimeZoneOption("UTC", "UTC"),
    TimeZoneOption("Asia/Shanghai", "Shanghai"),
    TimeZoneOption("Asia/Tokyo", "Tokyo"),
    TimeZoneOption("Asia/Singapore", "Singapore"),
    TimeZoneOption("Europe/London", "London"),
    TimeZoneOption("Europe/Paris", "Paris"),
    TimeZoneOption("America/New_York", "New York"),
    TimeZoneOption("America/Los_Angeles", "Los Angeles"),
)

SUPPORTED_TIME_ZONES = frozenset(option.identifier for option in TIME_ZONE_OPTIONS)


def validate_time_zone(timezone: str) -> str:
    """Return one supported IANA zone or raise a user-facing validation error."""

    if timezone not in SUPPORTED_TIME_ZONES:
        raise ValueError(f"Unsupported time zone: {timezone}")
    return timezone
