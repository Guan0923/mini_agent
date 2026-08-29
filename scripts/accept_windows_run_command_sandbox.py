"""Real, local acceptance matrix for the Windows run_command sandbox.

Run from an initialized checkout with ``conda activate dev`` followed by
``uv run python scripts/accept_windows_run_command_sandbox.py``.  The script
uses only a temporary workspace and a loopback HTTP server; it never calls a
paid or external service.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend.sandbox import FileAccessMode, NetworkMode, NetworkRule, SandboxLauncher, SandboxPolicy
from backend.sandbox.control.broker import WindowsBrokerClient
from backend.sandbox.runtime.audit import WritablePathAudit


class _LoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        body = b"sandbox-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _ControlledRuntimeAuditor:
    """Limit a runtime smoke to paths created by this acceptance process."""

    @staticmethod
    def scan(**_kwargs: object) -> WritablePathAudit:
        return WritablePathAudit((), {}, 0)


def _run(
    launcher: SandboxLauncher,
    policy: SandboxPolicy,
    command: str,
) -> tuple[int, str, str]:
    return _run_argv(
        launcher,
        policy,
        [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", command],
    )


def _run_argv(
    launcher: SandboxLauncher,
    policy: SandboxPolicy,
    argv: list[str],
) -> tuple[int, str, str]:
    process = launcher.launch(
        argv,
        policy,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=15)
        return (
            process.returncode or 0,
            (stdout or b"").decode(errors="replace"),
            (stderr or b"").decode(errors="replace"),
        )
    finally:
        if not launcher.cleanup(process):
            raise RuntimeError("sandbox acceptance cleanup failed")


def _compile_network_probe(workspace: Path) -> Path:
    compiler = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    source = Path(__file__).with_name("sandbox_network_probe.cs").resolve()
    output = workspace / "sandbox-network-probe.exe"
    result = subprocess.run(
        [str(compiler), "/nologo", "/target:exe", f"/out:{output}", str(source)],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or not output.is_file():
        raise RuntimeError("sandbox network probe compilation failed")
    return output


def _network_command(probe: Path, target_port: int, *, use_proxy: bool) -> str:
    mode = "proxy" if use_proxy else "direct"
    return f'"{probe}" {target_port} {mode}'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--controlled-runtime-smoke",
        action="store_true",
        help="exercise Broker/Token/process/network behavior without the machine-wide path audit",
    )
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise RuntimeError("Windows sandbox acceptance is available only on Windows")
    broker = WindowsBrokerClient.from_system(expected_proxy_port=17831)
    status = broker.status()
    if not status.healthy:
        raise RuntimeError("Windows Broker is not healthy")
    launcher = SandboxLauncher(
        broker=broker,
        is_windows=True,
        path_auditor=_ControlledRuntimeAuditor() if args.controlled_runtime_smoke else None,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    target_port = int(server.server_address[1])
    results: list[dict[str, object]] = []
    outside = Path(tempfile.gettempdir()).resolve().parent / f"mini-agent-outside-{uuid.uuid4().hex}.tmp"
    try:
        with tempfile.TemporaryDirectory(prefix="mini-agent-real-acceptance-") as temporary:
            workspace = Path(temporary).resolve()
            network_probe = _compile_network_probe(workspace)
            if args.controlled_runtime_smoke:
                powershell_results: list[dict[str, object]] = []
                for smoke_mode in FileAccessMode:
                    smoke_policy = SandboxPolicy(
                        workspace,
                        "runtime-smoke",
                        f"powershell-{smoke_mode.value}-{uuid.uuid4().hex}",
                        file_mode=smoke_mode,
                        network_mode=NetworkMode.FULL_NETWORK,
                    )
                    powershell_rc, powershell_output, powershell_error = _run_argv(
                        launcher,
                        smoke_policy,
                        [
                            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                            "-NoLogo",
                            "-NoProfile",
                            "-NonInteractive",
                            "-Command",
                            "Write-Output powershell-ok",
                        ],
                    )
                    powershell_results.append(
                        {
                            "file_mode": smoke_mode.value,
                            "rc": powershell_rc,
                            "passed": powershell_rc == 0 and "powershell-ok" in powershell_output,
                            "error": powershell_error.strip()[:500],
                        }
                    )
                if not all(bool(result["passed"]) for result in powershell_results):
                    print(
                        json.dumps(
                            {
                                "status": "failed",
                                "runtime_smoke": {"powershell": powershell_results},
                            },
                            ensure_ascii=True,
                            indent=2,
                        )
                    )
                    return 1
            for file_mode in FileAccessMode:
                for network_mode in NetworkMode:
                    suffix = f"{file_mode.value}-{network_mode.value}-{uuid.uuid4().hex[:8]}"
                    rules = (
                        (NetworkRule("127.0.0.1", target_port),)
                        if network_mode is NetworkMode.RESTRICTED_NETWORK
                        else ()
                    )

                    def policy(label: str) -> SandboxPolicy:
                        return SandboxPolicy(
                            workspace,
                            "real-acceptance",
                            f"{suffix}-{label}",
                            file_mode=file_mode,
                            network_mode=network_mode,
                            network_allowlist=rules,
                        )

                    workspace_probe = workspace / f"workspace-{suffix}.tmp"
                    workspace_rc, _, workspace_error = _run(
                        launcher, policy("workspace"), f'echo workspace-ok>"{workspace_probe}"'
                    )
                    temp_rc, _, temp_error = _run(launcher, policy("temp"), 'echo temp-ok>"%TEMP%\\temp-probe.tmp"')
                    outside_rc, _, outside_error = _run(launcher, policy("outside"), f'echo outside-probe>"{outside}"')
                    identity_rc, identity, identity_error = _run(
                        launcher,
                        policy("identity"),
                        'whoami & echo TEMP=%TEMP% & icacls "%TEMP%" & whoami /groups',
                    )
                    proxy_rc, proxy_body, proxy_error = _run(
                        launcher,
                        policy("proxy"),
                        _network_command(
                            network_probe,
                            target_port,
                            use_proxy=network_mode is not NetworkMode.FULL_NETWORK,
                        ),
                    )
                    direct_rc, direct_body, direct_error = _run(
                        launcher,
                        policy("direct"),
                        _network_command(network_probe, target_port, use_proxy=False),
                    )
                    row = {
                        "file_mode": file_mode.value,
                        "network_mode": network_mode.value,
                        "workspace_write": workspace_rc == 0 and workspace_probe.exists(),
                        "temp_write": temp_rc == 0,
                        "outside_write": outside_rc == 0 and outside.exists(),
                        "identity": identity.splitlines()[0].strip().casefold() if identity.splitlines() else "",
                        "proxy_request": proxy_rc == 0 and "sandbox-ok" in proxy_body,
                        "direct_request": direct_rc == 0 and "sandbox-ok" in direct_body,
                        "diagnostic": {
                            "workspace_rc": workspace_rc,
                            "temp_rc": temp_rc,
                            "outside_rc": outside_rc,
                            "identity_rc": identity_rc,
                            "proxy_rc": proxy_rc,
                            "direct_rc": direct_rc,
                            "workspace_error": workspace_error.strip()[:200],
                            "temp_error": temp_error.strip()[:200],
                            "outside_error": outside_error.strip()[:200],
                            "identity_error": identity_error.strip()[:200],
                            "proxy_error": proxy_error.strip()[:500],
                            "direct_error": direct_error.strip()[:500],
                            "identity_output": identity.strip()[:3000],
                        },
                    }
                    if outside.exists():
                        outside.unlink()
                    expected_workspace = file_mode is not FileAccessMode.READ_ONLY
                    expected_proxy = network_mode is not NetworkMode.NO_NETWORK
                    expected_direct = network_mode is NetworkMode.FULL_NETWORK
                    expected_identity = (
                        "codexsandboxonline" if network_mode is NetworkMode.FULL_NETWORK else "codexsandboxoffline"
                    )
                    checks = (
                        row["workspace_write"] is expected_workspace,
                        row["temp_write"] is True,
                        row["outside_write"] is False,
                        identity_rc == 0 and str(row["identity"]).endswith(expected_identity),
                        row["proxy_request"] is expected_proxy,
                        row["direct_request"] is expected_direct,
                    )
                    results.append(row)
                    if not all(checks):
                        print(json.dumps({"status": "failed", "case": row}, ensure_ascii=True, indent=2))
                        return 1
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        if outside.exists():
            outside.unlink()
    print(json.dumps({"status": "passed", "cases": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
