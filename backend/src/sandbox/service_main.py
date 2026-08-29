"""Windows Service entry point for the standalone Sandbox Broker."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _bootstrap_pywin32_paths() -> None:
    """Expose pywin32 modules to the embedded ``pythonservice.exe`` host."""

    runtime_root = Path(sys.executable).resolve().parent
    site_packages = runtime_root / "Lib" / "site-packages"
    for path in (site_packages, site_packages / "win32", site_packages / "win32" / "lib"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


_bootstrap_pywin32_paths()

from .broker_service import BrokerConfiguration, WindowsBrokerService, WindowsNamedPipeServer  # noqa: E402
from .errors import SandboxInitializationError  # noqa: E402
from .native_windows import windows_pipe_security_attributes, windows_service_sid  # noqa: E402

if os.name == "nt":
    import servicemanager  # type: ignore[import-not-found]
    import win32event  # type: ignore[import-not-found]
    import win32service  # type: ignore[import-not-found]
    import win32serviceutil  # type: ignore[import-not-found]
else:  # pragma: no cover - the service class is loaded only by Windows SCM
    servicemanager = None
    win32event = None
    win32service = None
    win32serviceutil = None


def _configuration() -> BrokerConfiguration:
    program_data = os.environ.get("MINI_AGENT_SANDBOX_PROGRAM_DATA")
    return BrokerConfiguration.create(program_data=Path(program_data) if program_data else None)


def _server() -> WindowsNamedPipeServer:
    configuration = _configuration()
    service = WindowsBrokerService(configuration)
    service.initialize()
    try:
        backend_sid = configuration.backend_sid_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise SandboxInitializationError("Broker backend SID is unavailable") from exc
    service_sid = windows_service_sid("MiniAgentSandboxBroker")
    return WindowsNamedPipeServer(
        service,
        # The service SID is required for subsequent CreateNamedPipe calls;
        # the backend SID remains the only non-privileged client grant.
        security_attributes_factory=lambda: windows_pipe_security_attributes(backend_sid, service_sid),
    )


if win32serviceutil is not None:

    class MiniAgentSandboxBrokerService(win32serviceutil.ServiceFramework):
        """Top-level pywin32 service class loaded by ``pythonservice.exe``."""

        _svc_name_ = "MiniAgentSandboxBroker"
        _svc_display_name_ = "Mini-Agent Sandbox Broker"
        _svc_description_ = "Privileged control plane for Mini-Agent Windows sandbox jobs."

        def __init__(self, args: Any) -> None:
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.server: WindowsNamedPipeServer | None = None

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.server is not None:
                self.server.close()
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self) -> None:
            servicemanager.LogInfoMsg("Mini-Agent Sandbox Broker starting")
            self.server = _server()
            self.server.serve_forever(
                stop=lambda: win32event.WaitForSingleObject(self.stop_event, 0) == win32event.WAIT_OBJECT_0
            )

else:

    class MiniAgentSandboxBrokerService:  # pragma: no cover - import compatibility on non-Windows
        pass


def main() -> int:
    if os.name != "nt":
        raise SandboxInitializationError("Windows Sandbox Broker cannot run on this platform")
    if servicemanager is None or win32serviceutil is None:
        raise SandboxInitializationError("pywin32 service support is unavailable")

    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "run":
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(MiniAgentSandboxBrokerService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    win32serviceutil.HandleCommandLine(MiniAgentSandboxBrokerService)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MiniAgentSandboxBrokerService", "main"]
