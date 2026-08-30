"""Real, local acceptance matrix for the Windows run_command sandbox.

Run from an initialized checkout with ``conda activate dev`` followed by
``uv run python scripts/accept_windows_run_command_sandbox.py``.  The script
uses only a temporary workspace and a loopback HTTP server; it never calls a
paid or external service.
"""

from __future__ import annotations

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


class _LoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        body = b"sandbox-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


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


def _network_command(probe: Path, target_host: str, target_port: int, *, use_proxy: bool) -> str:
    mode = "proxy" if use_proxy else "direct"
    return f'"{probe}" {target_host} {target_port} {mode}'


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("Windows sandbox acceptance is available only on Windows")
    broker = WindowsBrokerClient.from_system(expected_proxy_port=17831)
    status = broker.status()
    if not status.healthy:
        raise RuntimeError("Windows Broker is not healthy")
    launcher = SandboxLauncher(broker=broker, is_windows=True)
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler) for _ in range(2)]
    server_threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for server_thread in server_threads:
        server_thread.start()
    target_ports = [int(server.server_address[1]) for server in servers]
    results: list[dict[str, object]] = []
    outside = Path(tempfile.gettempdir()).resolve().parent / f"mini-agent-outside-{uuid.uuid4().hex}.tmp"
    try:
        with tempfile.TemporaryDirectory(prefix="mini-agent-real-acceptance-") as temporary:
            workspace = Path(temporary).resolve()
            network_probe = _compile_network_probe(workspace)
            powershell_results: list[dict[str, object]] = []
            for smoke_mode in FileAccessMode:
                smoke_policy = SandboxPolicy(
                    (workspace,),
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
                    rules = (NetworkRule("127.0.0.1"),) if network_mode is NetworkMode.RESTRICTED_NETWORK else ()

                    def policy(label: str) -> SandboxPolicy:
                        return SandboxPolicy(
                            (workspace,),
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
                    proxy_results = [
                        _run(
                            launcher,
                            policy(f"proxy-{index}"),
                            _network_command(
                                network_probe,
                                "127.0.0.1",
                                target_port,
                                use_proxy=network_mode is not NetworkMode.FULL_NETWORK,
                            ),
                        )
                        for index, target_port in enumerate(target_ports)
                    ]
                    alias_rc, alias_body, alias_error = _run(
                        launcher,
                        policy("alias"),
                        _network_command(
                            network_probe,
                            "localhost",
                            target_ports[0],
                            use_proxy=network_mode is not NetworkMode.FULL_NETWORK,
                        ),
                    )
                    direct_rc, direct_body, direct_error = _run(
                        launcher,
                        policy("direct"),
                        _network_command(network_probe, "127.0.0.1", target_ports[0], use_proxy=False),
                    )
                    proxy_requests = [rc == 0 and "sandbox-ok" in body for rc, body, _error in proxy_results]
                    row = {
                        "file_mode": file_mode.value,
                        "network_mode": network_mode.value,
                        "workspace_write": workspace_rc == 0 and workspace_probe.exists(),
                        "temp_write": temp_rc == 0,
                        "outside_write": outside_rc == 0 and outside.exists(),
                        "identity": identity.splitlines()[0].strip().casefold() if identity.splitlines() else "",
                        "proxy_requests": proxy_requests,
                        "unlisted_alias_request": alias_rc == 0 and "sandbox-ok" in alias_body,
                        "direct_request": direct_rc == 0 and "sandbox-ok" in direct_body,
                        "diagnostic": {
                            "workspace_rc": workspace_rc,
                            "temp_rc": temp_rc,
                            "outside_rc": outside_rc,
                            "identity_rc": identity_rc,
                            "proxy_rcs": [rc for rc, _body, _error in proxy_results],
                            "alias_rc": alias_rc,
                            "direct_rc": direct_rc,
                            "workspace_error": workspace_error.strip()[:200],
                            "temp_error": temp_error.strip()[:200],
                            "outside_error": outside_error.strip()[:200],
                            "identity_error": identity_error.strip()[:200],
                            "proxy_errors": [error.strip()[:500] for _rc, _body, error in proxy_results],
                            "alias_error": alias_error.strip()[:500],
                            "direct_error": direct_error.strip()[:500],
                            "identity_output": identity.strip()[:3000],
                        },
                    }
                    if outside.exists():
                        outside.unlink()
                    expected_workspace = file_mode is not FileAccessMode.READ_ONLY
                    expected_proxy = network_mode is not NetworkMode.NO_NETWORK
                    expected_direct = network_mode is NetworkMode.FULL_NETWORK
                    expected_alias = network_mode is NetworkMode.FULL_NETWORK
                    expected_identity = (
                        "codexsandboxonline" if network_mode is NetworkMode.FULL_NETWORK else "codexsandboxoffline"
                    )
                    checks = (
                        row["workspace_write"] is expected_workspace,
                        row["temp_write"] is True,
                        row["outside_write"] is False,
                        identity_rc == 0 and str(row["identity"]).endswith(expected_identity),
                        all(result is expected_proxy for result in proxy_requests),
                        row["unlisted_alias_request"] is expected_alias,
                        row["direct_request"] is expected_direct,
                    )
                    results.append(row)
                    if not all(checks):
                        print(json.dumps({"status": "failed", "case": row}, ensure_ascii=True, indent=2))
                        return 1
            port_policy = SandboxPolicy(
                (workspace,),
                "real-acceptance",
                f"restricted-port-{uuid.uuid4().hex}",
                file_mode=FileAccessMode.READ_ONLY,
                network_mode=NetworkMode.RESTRICTED_NETWORK,
                network_allowlist=(NetworkRule("127.0.0.1", target_ports[0]),),
            )
            allowed_rc, allowed_body, allowed_error = _run(
                launcher,
                port_policy,
                _network_command(network_probe, "127.0.0.1", target_ports[0], use_proxy=True),
            )
            denied_policy = SandboxPolicy(
                (workspace,),
                "real-acceptance",
                f"restricted-port-denied-{uuid.uuid4().hex}",
                file_mode=FileAccessMode.READ_ONLY,
                network_mode=NetworkMode.RESTRICTED_NETWORK,
                network_allowlist=(NetworkRule("127.0.0.1", target_ports[0]),),
            )
            denied_rc, denied_body, denied_error = _run(
                launcher,
                denied_policy,
                _network_command(network_probe, "127.0.0.1", target_ports[1], use_proxy=True),
            )
            port_result = {
                "allowed": allowed_rc == 0 and "sandbox-ok" in allowed_body,
                "denied": denied_rc != 0 and "sandbox-ok" not in denied_body,
                "allowed_error": allowed_error.strip()[:500],
                "denied_error": denied_error.strip()[:500],
            }
            if not all((port_result["allowed"], port_result["denied"])):
                print(json.dumps({"status": "failed", "restricted_port": port_result}, ensure_ascii=True, indent=2))
                return 1
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for server_thread in server_threads:
            server_thread.join(timeout=5)
        if outside.exists():
            outside.unlink()
    print(
        json.dumps(
            {"status": "passed", "powershell": powershell_results, "cases": results, "restricted_port": port_result},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
