"""Bounded per-user sandbox account lease pools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from ..errors import SandboxCleanupPending, SandboxInitializationError


@dataclass(frozen=True, slots=True)
class AccountLease:
    user_id: str
    job_id: str
    account: str
    sid: str
    kind: str


class AccountPool:
    """A bounded per-user account lease pool with cleanup-before-reuse."""

    def __init__(self, user_id: str, kind: str, accounts: tuple[tuple[str, str], ...]) -> None:
        if kind not in {"command", "mcp"}:
            raise ValueError("account pool kind must be command or mcp")
        if len(accounts) > 4:
            raise ValueError("an account pool may contain at most four accounts")
        self.user_id = user_id
        self.kind = kind
        self._accounts = tuple(accounts)
        self._active: dict[str, AccountLease] = {}
        self._lock = RLock()

    def acquire(self, job_id: str) -> AccountLease:
        if not isinstance(job_id, str) or not job_id:
            raise SandboxInitializationError("account lease Job id is invalid")
        with self._lock:
            if job_id in self._active:
                raise SandboxInitializationError("Job already owns an account lease")
            used = {lease.account for lease in self._active.values()}
            account = next(((name, sid) for name, sid in self._accounts if name not in used), None)
            if account is None:
                raise SandboxInitializationError("sandbox account pool is exhausted")
            lease = AccountLease(self.user_id, job_id, account[0], account[1], self.kind)
            self._active[job_id] = lease
            return lease

    def release(self, job_id: str, cleanup: Callable[[AccountLease], bool]) -> None:
        with self._lock:
            lease = self._active.get(job_id)
        if lease is None:
            return
        try:
            complete = bool(cleanup(lease))
        except Exception:
            complete = False
        if not complete:
            raise SandboxCleanupPending("sandbox account cleanup is pending")
        with self._lock:
            self._active.pop(job_id, None)

    def active(self) -> tuple[AccountLease, ...]:
        with self._lock:
            return tuple(self._active.values())


@dataclass(slots=True)
class UserAccountPools:
    command: AccountPool
    mcp: AccountPool

    @classmethod
    def create(
        cls,
        user_id: str,
        *,
        command_accounts: tuple[tuple[str, str], ...] = (),
        mcp_accounts: tuple[tuple[str, str], ...] = (),
    ) -> UserAccountPools:
        return cls(
            command=AccountPool(user_id, "command", command_accounts),
            mcp=AccountPool(user_id, "mcp", mcp_accounts),
        )
