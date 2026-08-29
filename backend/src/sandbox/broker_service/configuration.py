"""Installation-scoped Broker paths and persisted identity."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..errors import SandboxInitializationError
from .protocol import _atomic_temporary, _default_program_data


@dataclass(frozen=True, slots=True)
class BrokerConfiguration:
    """Installation-scoped Broker paths and identities."""

    installation_id: str
    backend_instance_id: str
    program_data: Path
    pipe_name: str = r"\\.\pipe\mini-agent-sandbox-broker"

    @classmethod
    def create(
        cls,
        *,
        program_data: Path | None = None,
        installation_id: str | None = None,
        backend_instance_id: str | None = None,
        pipe_name: str = r"\\.\pipe\mini-agent-sandbox-broker",
    ) -> BrokerConfiguration:
        resolved_program_data = Path(program_data or _default_program_data())
        if installation_id is None:
            try:
                persisted_id = (resolved_program_data / "installation.id").read_text(encoding="ascii").strip()
            except OSError:
                persisted_id = ""
            installation_id = persisted_id or f"install-{uuid.uuid4().hex}"
        return cls(
            installation_id=installation_id,
            backend_instance_id=backend_instance_id or f"backend-{uuid.uuid4().hex}",
            program_data=resolved_program_data,
            pipe_name=pipe_name,
        )

    @property
    def manifest_path(self) -> Path:
        return self.program_data / "resources.json"

    @property
    def installation_key_path(self) -> Path:
        return self.program_data / "installation.key.dpapi"

    @property
    def installation_id_path(self) -> Path:
        return self.program_data / "installation.id"

    @property
    def backend_sid_path(self) -> Path:
        return self.program_data / "backend.sid"

    @property
    def credential_path(self) -> Path:
        return self.program_data / "accounts.dpapi"

    @property
    def ready_path(self) -> Path:
        return self.program_data / "ready.json"

    @property
    def audit_path(self) -> Path:
        return self.program_data / "control-plane.jsonl"

    def persist_installation_id(self) -> None:
        self.program_data.mkdir(parents=True, exist_ok=True)
        try:
            existing = self.installation_id_path.read_text(encoding="ascii").strip()
        except OSError:
            existing = ""
        if existing:
            if existing != self.installation_id:
                raise SandboxInitializationError("Broker installation identity does not match ProgramData")
            return
        fd, temporary = _atomic_temporary(self.program_data, ".installation.id.")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(self.installation_id)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.installation_id_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
