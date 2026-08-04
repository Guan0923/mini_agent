"""Independent, subprocess-backed verifiers for source-adapted tasks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from ..model import CheckContext, CheckerVerdict

_SCRIPT = Path(__file__).resolve().parent.parent / "verifiers" / "verify.py"
_MAX_OUTPUT = 20_000


def _verifier_environment() -> dict[str, str]:
    """Return only stream/runtime variables needed by an isolated child.

    Windows' ``_overlapped`` extension reads ``SystemRoot`` directly from the
    process environment even when Python is launched with ``-I``.  Keeping
    those OS paths while omitting credentials, proxy settings, and the rest of
    the parent's environment preserves isolation without breaking asyncio.
    """
    environment = {"PYTHONIOENCODING": "utf-8"}
    for name in ("SystemRoot", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def python_verifier(name: str, *, timeout_seconds: int = 30) -> Callable[[CheckContext], CheckerVerdict]:
    """Create a checker that runs one stdlib-only verifier in an isolated process."""

    def _check(context: CheckContext) -> CheckerVerdict:
        command = [
            sys.executable,
            "-I",
            str(_SCRIPT),
            name,
            str(context.workspace),
            "--final-answer",
            context.final_answer,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=context.workspace,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                env=_verifier_environment(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CheckerVerdict(0.0, detail=f"verifier timed out after {timeout_seconds}s")
        except OSError as exc:
            return CheckerVerdict(0.0, detail=f"verifier could not start: {type(exc).__name__}: {exc}")

        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        if len(stdout) > _MAX_OUTPUT or len(stderr) > _MAX_OUTPUT:
            return CheckerVerdict(0.0, detail=f"verifier output exceeded {_MAX_OUTPUT} characters")
        if completed.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"verifier exited with code {completed.returncode}"
            return CheckerVerdict(0.0, detail=detail)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return CheckerVerdict(0.0, detail="verifier returned invalid JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
            return CheckerVerdict(0.0, detail="verifier returned an invalid result shape")
        detail = str(payload.get("detail") or ("verifier passed" if payload["passed"] else "verifier failed"))
        return CheckerVerdict(1.0 if payload["passed"] else 0.0, detail=detail)

    return _check


__all__ = ["python_verifier"]
