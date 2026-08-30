"""Session-bound, redacted approval records for sandbox permission changes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import Protocol

from ..policy import FileAccessMode


class ApprovalDecision(StrEnum):
    DENY = "deny"
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"


def authorization_hash(
    *, session_id: str, command: str, cwd: str, permission_target: str, network_target: str = ""
) -> str:
    """Hash approval identity; raw command/environment values are not stored."""

    payload = {
        "session_id": session_id,
        "command": command,
        "cwd": cwd,
        "permission_target": permission_target,
        "network_target": network_target,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _field_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _redacted_summary(value: str) -> str:
    return f"sha256:{_field_hash(value)[:16]}:length:{len(value)}"


class ApprovalRepository(Protocol):
    def save_sandbox_approval(
        self,
        session_id: str,
        request_hash: str,
        command_hash: str,
        cwd_hash: str,
        permission_target: str,
        network_target_hash: str,
        command_summary: str,
        cwd_summary: str,
    ) -> None: ...

    def has_sandbox_approval(self, session_id: str, request_hash: str, permission_target: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    session_id: str
    request_hash: str
    permission_target: str
    decision: ApprovalDecision
    command_hash: str = ""
    cwd_hash: str = ""

    def to_public(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "request_hash": self.request_hash,
            "permission_target": self.permission_target,
            "decision": self.decision.value,
        }


class ApprovalStore:
    """Session grant cache backed by optional local-only persistence.

    Session grants intentionally have no revoke operation. A new session id is
    the lifecycle boundary specified by the product contract.
    """

    def __init__(self, repository: ApprovalRepository | None = None) -> None:
        self._lock = RLock()
        self._session_grants: dict[tuple[str, str, str], ApprovalGrant] = {}
        self._repository = repository

    def decide(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        permission_target: FileAccessMode | str,
        network_target: str = "",
        decision: ApprovalDecision | str,
    ) -> ApprovalGrant | None:
        choice = ApprovalDecision(str(decision))
        target = str(permission_target)
        digest = authorization_hash(
            session_id=session_id,
            command=command,
            cwd=cwd,
            permission_target=target,
            network_target=network_target,
        )
        if choice is ApprovalDecision.DENY:
            return None
        command_hash = _field_hash(command)
        cwd_hash = _field_hash(cwd)
        grant = ApprovalGrant(session_id, digest, target, choice, command_hash, cwd_hash)
        if choice is ApprovalDecision.ALLOW_SESSION:
            with self._lock:
                self._session_grants[(session_id, digest, target)] = grant
            if self._repository is not None:
                self._repository.save_sandbox_approval(
                    session_id,
                    digest,
                    command_hash,
                    cwd_hash,
                    target,
                    _field_hash(network_target),
                    _redacted_summary(command),
                    _redacted_summary(cwd),
                )
        return grant

    def allowed(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        permission_target: FileAccessMode | str,
        network_target: str = "",
    ) -> bool:
        digest = authorization_hash(
            session_id=session_id,
            command=command,
            cwd=cwd,
            permission_target=str(permission_target),
            network_target=network_target,
        )
        with self._lock:
            cached = (session_id, digest, str(permission_target)) in self._session_grants
        if cached:
            return True
        if self._repository is None:
            return False
        return self._repository.has_sandbox_approval(session_id, digest, str(permission_target))


__all__ = ["ApprovalDecision", "ApprovalGrant", "ApprovalRepository", "ApprovalStore", "authorization_hash"]
