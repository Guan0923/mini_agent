"""DPAPI adapter and atomic installation-key storage."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from threading import RLock
from typing import Protocol

from ..errors import SandboxInitializationError
from .protocol import _atomic_temporary


class DpapiProvider(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class WindowsDpapiProvider:
    """Thin pywin32 wrapper; importing this class is safe off Windows."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SandboxInitializationError("DPAPI is available only on Windows")
        try:
            import win32crypt  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SandboxInitializationError("pywin32 is required for Broker DPAPI") from exc
        self._win32crypt = win32crypt

    def protect(self, value: bytes) -> bytes:
        try:
            result = self._win32crypt.CryptProtectData(
                value,
                "Mini-Agent Sandbox Broker",
                None,
                None,
                None,
                0x4,
            )
            blob = result[1] if isinstance(result, tuple) else result
            return bytes(blob)
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("DPAPI could not protect the Broker key") from exc

    def unprotect(self, value: bytes) -> bytes:
        try:
            result = self._win32crypt.CryptUnprotectData(value, None, None, None, 0)
            blob = result[1] if isinstance(result, tuple) else result
            return bytes(blob)
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("DPAPI could not unprotect the Broker key") from exc


class DpapiKeyStore:
    """Atomically persist an installation key as DPAPI ciphertext."""

    def __init__(self, path: Path, *, provider: DpapiProvider | None = None) -> None:
        self.path = Path(path)
        self.provider = provider
        self._lock = RLock()

    def load(self) -> bytes:
        with self._lock:
            try:
                blob = self.path.read_bytes()
            except OSError as exc:
                raise SandboxInitializationError("Broker installation key is unavailable") from exc
        if not blob:
            raise SandboxInitializationError("Broker installation key is empty")
        provider = self._provider()
        key = provider.unprotect(blob)
        if len(key) < 32:
            raise SandboxInitializationError("Broker installation key is invalid")
        return key

    def ensure(self) -> bytes:
        with self._lock:
            if self.path.exists():
                try:
                    if self.path.stat().st_size > 0:
                        return self.load()
                except OSError as exc:
                    raise SandboxInitializationError("Broker installation key is unavailable") from exc
            key = secrets.token_bytes(32)
            protected = self._provider().protect(key)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = _atomic_temporary(self.path.parent, f".{self.path.name}.")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(protected)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                if os.name != "nt":
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        pass
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            return key

    def _provider(self) -> DpapiProvider:
        if self.provider is not None:
            return self.provider
        return WindowsDpapiProvider()
