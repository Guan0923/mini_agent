"""Privileged WFP controller contract used by the Broker adapter."""

from __future__ import annotations

from typing import Protocol

from ..policy import NetworkMode


class WfpController(Protocol):
    """Privileged WFP provider; rules are always fixed IP/port tuples."""

    def apply(
        self,
        *,
        rule_id: str,
        account_sid: str,
        mode: NetworkMode,
        endpoints: tuple[tuple[str, int], ...],
    ) -> tuple[str, ...]: ...

    def remove(self, rule_ids: tuple[str, ...]) -> bool: ...
