"""Fork completed local or remotely synchronized runs into writable sessions."""

from __future__ import annotations

import shutil
from pathlib import Path

from backend.domain import RunProvenance, Session, new_run_id
from backend.domain.runtime_state import NodeWriter, message_payload, resolve_fork_anchor
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
                row = source.execute(
                    "SELECT s.status, s.task, r.state_json FROM session_runs AS s "
                    "LEFT JOIN runs AS r ON r.run_id=s.run_id WHERE s.run_id=?",
                    (run_id,),
                ).fetchone()
            if row is None:
                continue
            if row[0] == "running":
                raise ValueError("A running run cannot be forked.")
            nodes = self.load_nodes(summary.session_id)
            meaningful_nodes = [node for node in nodes if node.data_type != "root"]
            if not meaningful_nodes:
                # A v4 database can still contain a run created by a legacy
                # embedding caller that has not emitted a message-tree node.
                # Keep that run forkable through the non-authoritative
                # execution projection; newly-created conversations always
                # use the node path below.
                if row[2] is None:
                    raise ValueError("Session has no RuntimeState nodes to fork.")
                state = decode_runtime_state(str(row[2]))
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
                try:
                    # Legacy embedding callers may have persisted only the
                    # execution projection.  Rebuild its text transcript as
                    # canonical v4 nodes before saving the fork.  The target
                    # starts a new tree: there is no source node to reference
                    # and no legacy row is allowed to become model context.
                    writer = NodeWriter(self)
                    parent = self.get_session_root(target.session_id)
                    if parent is None:
                        raise RuntimeError("Fork target root was not created.")
                    for item in text_messages(state.messages):
                        node = writer.create(
                            session_id=target.session_id,
                            parent=parent,
                            data=message_payload(item["role"], item["content"]),
                            provider_name=state.provider_name or state.provider,
                            model=state.model_snapshot,
                            permission_mode=state.permission_mode,
                            running_mode=state.running_mode,
                            cwd=state.workspace_root or "",
                        )
                        parent = writer.delete(node.session_id, node.id)
                    self._save_state(state, "forked")
                    self.paths.ensure_session(target.session_id)
                    _copy_tree_without_symlinks(
                        self.paths.session_workspace(summary.session_id),
                        self.paths.session_workspace(target.session_id),
                    )
                    _copy_tree_without_symlinks(
                        self.paths.session_uploads(summary.session_id),
                        self.paths.session_uploads(target.session_id),
                    )
                except Exception:
                    shutil.rmtree(self.paths.session_root(target.session_id), ignore_errors=True)
                    raise
                return target
            source_leaf = max(nodes, key=lambda item: (item.timestamp, item.id))
            source_anchor = resolve_fork_anchor(source_leaf, self.get_node)
            if source_anchor.key not in {node.key for node in nodes}:
                raise ValueError("Fork source parent does not belong to the source ancestry tree.")
            target = self.create_session(
                f"Fork: {summary.title}",
                local_only=summary.local_only,
                root_parent=source_anchor.key,
            )
            new_id = new_run_id()
            provenance = RunProvenance(
                workflow_id=new_id,
                trigger="legacy",
                source_session_id=summary.session_id,
                source_run_id=run_id,
            )
            self.start_turn(
                target.session_id,
                new_id,
                str(row[1]),
                provenance,
                append_user_message=False,
            )
            if row[2] is not None:
                state = decode_runtime_state(str(row[2]))
                if state.current_run is not None:
                    state.session_id = target.session_id
                    state.current_run.run_id = new_id
                    state.current_run.provenance = provenance
                    self._save_state(state, "forked")
            try:
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
