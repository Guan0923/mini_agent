"""Session-bound, redacted approval records for sandbox permission changes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock

from .policy import PermissionMode


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


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    session_id: str
    request_hash: str
    permission_target: str
    decision: ApprovalDecision

    def to_public(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "request_hash": self.request_hash,
            "permission_target": self.permission_target,
            "decision": self.decision.value,
        }


class ApprovalStore:
    """In-memory store suitable for one runtime process.

    Session grants intentionally have no revoke operation. A new session id is
    the lifecycle boundary specified by the product contract.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._session_grants: dict[tuple[str, str, str], ApprovalGrant] = {}

    def decide(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        permission_target: PermissionMode | str,
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
        grant = ApprovalGrant(session_id, digest, target, choice)
        if choice is ApprovalDecision.ALLOW_SESSION:
            with self._lock:
                self._session_grants[(session_id, digest, target)] = grant
        return grant

    def allowed(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        permission_target: PermissionMode | str,
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
            return (session_id, digest, str(permission_target)) in self._session_grants


__all__ = ["ApprovalDecision", "ApprovalGrant", "ApprovalStore", "authorization_hash"]
