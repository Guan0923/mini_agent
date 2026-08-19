"""Atomic Broker-owned resource manifest and conservative orphan recovery."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    installation_id: str
    backend_instance_id: str
    user_id: str
    job_id: str
    resources: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "installation_id": self.installation_id,
            "backend_instance_id": self.backend_instance_id,
            "user_id": self.user_id,
            "job_id": self.job_id,
            "resources": dict(self.resources),
        }


class ResourceManifest:
    """A small atomic JSON manifest suitable for ProgramData storage."""

    def __init__(self, path: Path, *, installation_id: str, backend_instance_id: str) -> None:
        self.path = Path(path)
        self.installation_id = installation_id
        self.backend_instance_id = backend_instance_id
        self._lock = RLock()

    def records(self) -> tuple[ResourceRecord, ...]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError):
                return ()
        values = raw.get("records", []) if isinstance(raw, dict) else []
        parsed: list[ResourceRecord] = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, Mapping) or not item.get("installation_id") or not item.get("job_id"):
                continue
            resources = item.get("resources")
            parsed.append(
                ResourceRecord(
                    str(item.get("installation_id")),
                    str(item.get("backend_instance_id") or ""),
                    str(item.get("user_id") or ""),
                    str(item.get("job_id")),
                    dict(resources) if isinstance(resources, Mapping) else {},
                )
            )
        return tuple(parsed)

    def register(
        self,
        user_id: str,
        job_id: str,
        resources: Mapping[str, object],
        *,
        backend_instance_id: str | None = None,
    ) -> ResourceRecord:
        with self._lock:
            backend_id = backend_instance_id or self.backend_instance_id
            record = ResourceRecord(self.installation_id, backend_id, user_id, job_id, dict(resources))
            values = [
                item
                for item in self.records()
                if not (
                    item.installation_id == self.installation_id
                    and item.backend_instance_id == backend_id
                    and item.user_id == user_id
                    and item.job_id == job_id
                )
            ]
            values.append(record)
            self._write(values)
            return record

    def remove(self, user_id: str, job_id: str, *, backend_instance_id: str | None = None) -> None:
        with self._lock:
            backend_id = backend_instance_id or self.backend_instance_id
            self._write(
                [
                    item
                    for item in self.records()
                    if not (
                        item.installation_id == self.installation_id
                        and item.backend_instance_id == backend_id
                        and item.user_id == user_id
                        and item.job_id == job_id
                    )
                ]
            )

    def owned_orphans(
        self,
        live_job_ids: set[str],
        *,
        backend_instance_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[ResourceRecord, ...]:
        """Return only records provably owned by this installation/backend."""

        return tuple(
            item
            for item in self.records()
            if item.installation_id == self.installation_id
            and item.backend_instance_id == (backend_instance_id or self.backend_instance_id)
            and (user_id is None or item.user_id == user_id)
            and item.job_id not in live_job_ids
        )

    def _write(self, values: list[ResourceRecord]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"records": [item.to_dict() for item in values]}, stream, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass


__all__ = ["ResourceManifest", "ResourceRecord"]
