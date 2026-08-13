"""Fork completed local or remotely synchronized runs into writable sessions."""

from __future__ import annotations

import shutil
from pathlib import Path

from backend.domain import RunProvenance, Session, new_run_id
from backend.domain.state import utc_now
from backend.runtime.core.context import text_messages

from .codec import decode_runtime_state


class SQLiteForkMixin:
    """Create locally owned sessions from terminal durable run snapshots."""

    def find_run_session(self, run_id: str):
        """Return the session owning a run without creating a fork."""

        for summary in self.list_sessions(state="all"):
            with self._connection(summary.session_id) as source:
                row = source.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is not None:
                return summary
        return None

    def list_forkable_runs(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for summary in self.list_sessions(state="all"):
            with self._connection(summary.session_id) as connection:
                rows = connection.execute(
                    "SELECT s.run_id,s.task,s.status,s.updated_at FROM session_runs AS s "
                    "JOIN runs AS r ON r.run_id=s.run_id "
                    "WHERE s.status!='running' AND r.status!='running' ORDER BY s.updated_at DESC"
                ).fetchall()
            result.extend(
                {
                    "run_id": str(row[0]),
                    "task": str(row[1]),
                    "status": str(row[2]),
                    "updated_at": str(row[3]),
                }
                for row in rows
            )
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def fork_run(self, run_id: str) -> Session:
        # Forking is an audit operation; a run remains forkable after its
        # source session has been archived or soft-deleted.
        for summary in self.list_sessions(state="all"):
            with self._connection(summary.session_id) as source:
                row = source.execute("SELECT status, state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                continue
            if row[0] == "running":
                raise ValueError("A running run cannot be forked.")
            state = decode_runtime_state(str(row[1]))
            if state.current_run is None:
                raise ValueError("Run snapshot cannot be forked.")
            target = self.create_session(f"Fork: {summary.title}", local_only=summary.local_only)
            state.session_id = target.session_id
            state.current_run.run_id = new_run_id()
            state.current_run.provenance = RunProvenance(
                workflow_id=state.current_run.provenance.workflow_id,
                trigger="legacy",
                source_session_id=summary.session_id,
                source_run_id=run_id,
            )
            self.start_turn(
                target.session_id,
                state.current_run.run_id,
                state.current_run.task,
                state.current_run.provenance,
                append_user_message=False,
            )
            with self._connection(target.session_id) as connection:
                timestamp = utc_now()
                for message in text_messages(state.messages):
                    connection.execute(
                        "INSERT INTO session_messages(run_id,role,content,created_at) VALUES (?,?,?,?)",
                        (state.current_run.run_id, message["role"], message["content"], timestamp),
                    )
            try:
                self._save_state(state, "forked")
                self.paths.ensure_session(target.session_id)
                if not summary.local_only:
                    _copy_tree_without_symlinks(
                        self.paths.session_workspace(summary.session_id),
                        self.paths.session_workspace(target.session_id),
                    )
                _copy_tree_without_symlinks(
                    self.paths.session_uploads(summary.session_id),
                    self.paths.session_uploads(target.session_id),
                )
            except Exception:
                # The target was created solely for this fork.  A failed
                # workspace/upload copy must not leave a discoverable,
                # partially initialized session behind.
                shutil.rmtree(self.paths.session_root(target.session_id), ignore_errors=True)
                raise
            return target
        raise ValueError(f"Unknown run: {run_id}")


def _copy_tree_without_symlinks(source: Path, target: Path) -> None:
    """Copy fork payloads without allowing workspace links to escape."""

    if source.is_symlink():
        raise ValueError("Session workspace cannot be a symbolic link.")
    if not source.is_dir():
        raise ValueError("Session workspace must be a directory.")
    if target.is_symlink():
        raise ValueError("Session target cannot be a symbolic link.")
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if item.is_symlink():
            raise ValueError("Session payload contains a symbolic link.")
        destination = target / relative
        if destination.is_symlink():
            raise ValueError("Session target contains a symbolic link.")
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
        else:
            raise ValueError("Session payload contains an unsupported special file.")
