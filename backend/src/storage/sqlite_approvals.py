"""Local-only session authorization persistence."""

from __future__ import annotations

from backend.domain.state import utc_now


class SQLiteApprovalMixin:
    """Persist sandbox grants in the owning session database only."""

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
    ) -> None:
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            meta = connection.execute(
                "SELECT 1 FROM json_objects WHERE session_id=? AND namespace='session' AND object_id=?",
                (session_id, session_id),
            ).fetchone()
            if meta is None:
                raise ValueError(f"Unknown session: {session_id}")
            connection.execute(
                """INSERT OR IGNORE INTO sandbox_approvals(
                    request_hash,session_id,command_hash,cwd_hash,permission_target,
                    network_target_hash,command_summary,cwd_summary,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    request_hash,
                    session_id,
                    command_hash,
                    cwd_hash,
                    permission_target,
                    network_target_hash,
                    command_summary,
                    cwd_summary,
                    utc_now(),
                ),
            )

    def has_sandbox_approval(self, session_id: str, request_hash: str, permission_target: str) -> bool:
        if not self.paths.session_db(session_id).exists():
            return False
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT 1 FROM sandbox_approvals WHERE session_id=? AND request_hash=? AND permission_target=?",
                (session_id, request_hash, permission_target),
            ).fetchone()
        return row is not None


__all__ = ["SQLiteApprovalMixin"]
