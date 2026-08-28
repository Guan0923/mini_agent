"""Synchronous authenticated Windows named-pipe server."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from typing import Any

from ..errors import SandboxInitializationError
from .service import WindowsBrokerService

logger = logging.getLogger(__name__)


class WindowsNamedPipeServer:
    """Minimal synchronous named-pipe loop for the standalone service.

    The security descriptor is supplied by the service installer, so the
    runtime never widens an existing ACL.  The adapter is intentionally lazy:
    importing the module on Linux or before pywin32 installation remains safe,
    while attempting to serve without those capabilities fails closed.
    """

    def __init__(
        self,
        service: WindowsBrokerService,
        *,
        pipe_handle_factory: Callable[[], Any] | None = None,
        security_attributes_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.service = service
        self.pipe_handle_factory = pipe_handle_factory
        self.security_attributes_factory = security_attributes_factory
        self._closed = False
        self._listener_lock = RLock()
        self._listener_handle: Any | None = None

    def serve_once(self) -> None:
        if os.name != "nt":
            raise SandboxInitializationError("Windows named pipes are unavailable on this platform")
        handle = self.pipe_handle_factory() if self.pipe_handle_factory is not None else self._create_pipe()
        try:
            import win32file  # type: ignore[import-not-found]
            import win32pipe  # type: ignore[import-not-found]

            win32pipe.ConnectNamedPipe(handle, None)
            _, payload = win32file.ReadFile(handle, 1024 * 1024)
            response = self.service.handle(payload)
            win32file.WriteFile(handle, response)
        except SandboxInitializationError:
            raise
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("Broker named-pipe request failed") from exc
        finally:
            try:
                import win32file  # type: ignore[import-not-found]

                win32file.CloseHandle(handle)
            except Exception:
                pass

    def serve_forever(self, *, stop: Callable[[], bool] | None = None) -> None:
        workers = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sandbox-broker-pipe")
        try:
            while not self._closed and not (stop is not None and stop()):
                handle = self.pipe_handle_factory() if self.pipe_handle_factory is not None else self._create_pipe()
                with self._listener_lock:
                    if self._closed:
                        self._close_handle(handle)
                        break
                    self._listener_handle = handle
                try:
                    import win32pipe  # type: ignore[import-not-found]

                    win32pipe.ConnectNamedPipe(handle, None)
                except Exception:
                    self._close_handle(handle)
                    if self._closed:
                        break
                    raise
                finally:
                    with self._listener_lock:
                        if self._listener_handle is handle:
                            self._listener_handle = None
                workers.submit(self._serve_connected, handle)
        finally:
            workers.shutdown(wait=True, cancel_futures=True)

    def close(self) -> None:
        self._closed = True
        self.service.close()
        with self._listener_lock:
            handle = self._listener_handle
            self._listener_handle = None
        if handle is not None:
            self._close_handle(handle)

    def _create_pipe(self) -> Any:
        try:
            import win32con  # type: ignore[import-not-found]
            import win32pipe  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("pywin32 is required for the Broker named pipe") from exc
        try:
            return win32pipe.CreateNamedPipe(
                self.service.configuration.pipe_name,
                win32con.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                1024 * 1024,
                1024 * 1024,
                0,
                self._security_attributes(),
            )
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            # The service host does not configure Python logging handlers, so
            # retain only the numeric Win32 failure in the Application log.
            # This makes field diagnosis possible without leaking the pipe
            # name, ACL/SDDL, or any filesystem paths.
            try:
                import servicemanager  # type: ignore[import-not-found]

                winerror = getattr(exc, "winerror", None)
                servicemanager.LogErrorMsg(f"Broker named-pipe creation failed (winerror={winerror!s})")
            except Exception:
                pass
            logger.error(
                "Broker named-pipe creation failed type=%s winerror=%s",
                type(exc).__name__,
                getattr(exc, "winerror", None),
                exc_info=False,
            )
            raise SandboxInitializationError("Broker named-pipe creation failed") from exc

    def _serve_connected(self, handle: Any) -> None:
        try:
            import win32file  # type: ignore[import-not-found]

            _, payload = win32file.ReadFile(handle, 1024 * 1024)
            win32file.WriteFile(handle, self.service.handle(payload))
        except Exception:
            logger.warning("Broker named-pipe request failed", exc_info=False)
        finally:
            self._close_handle(handle)

    @staticmethod
    def _close_handle(handle: Any) -> None:
        try:
            import win32file  # type: ignore[import-not-found]

            win32file.CloseHandle(handle)
        except Exception:
            pass

    def _security_attributes(self) -> Any:
        if self.security_attributes_factory is None:
            raise SandboxInitializationError("Broker named-pipe ACL is not configured")
        try:
            return self.security_attributes_factory()
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("Broker named-pipe ACL could not be created") from exc
