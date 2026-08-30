"""Non-sensitive Broker readiness marker contract."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path

from ..errors import SandboxInitializationError
from .credentials import BrokerCredentialPackage
from .protocol import BROKER_VERSION, _canonical

READY_SCHEMA = 3
TOKEN_MODEL = "capability_sid_v3"


def build_ready_marker(package: BrokerCredentialPackage, proxy_port: int) -> dict[str, object]:
    core = {
        "schema": READY_SCHEMA,
        "broker_version": BROKER_VERSION,
        "token_model": TOKEN_MODEL,
        "generation": package.generation,
        "proxy_port": proxy_port,
        "accounts": {
            "offline": {"name": package.offline_name, "sid": package.offline_sid},
            "online": {"name": package.online_name, "sid": package.online_sid},
        },
    }
    return {**core, "config_digest": hashlib.sha256(_canonical(core)).hexdigest()}


def validate_ready_marker(
    value: object,
    *,
    expected_proxy_port: int | None = None,
    package: BrokerCredentialPackage | None = None,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SandboxInitializationError("Broker ready marker is invalid")
    marker = dict(value)
    digest = marker.pop("config_digest", None)
    if (
        marker.get("schema") != READY_SCHEMA
        or marker.get("broker_version") != BROKER_VERSION
        or marker.get("token_model") != TOKEN_MODEL
        or not isinstance(digest, str)
        or not digest
        or not isinstance(marker.get("generation"), str)
        or isinstance(marker.get("proxy_port"), bool)
        or not isinstance(marker.get("proxy_port"), int)
    ):
        raise SandboxInitializationError("Broker ready marker is invalid")
    if not secrets_equal(digest, hashlib.sha256(_canonical(marker)).hexdigest()):
        raise SandboxInitializationError("Broker ready marker digest is invalid")
    if expected_proxy_port is not None and marker["proxy_port"] != expected_proxy_port:
        raise SandboxInitializationError("Broker proxy port requires repair")
    accounts = marker.get("accounts")
    if not isinstance(accounts, Mapping):
        raise SandboxInitializationError("Broker ready marker accounts are invalid")
    if package is not None:
        expected = build_ready_marker(package, int(marker["proxy_port"]))
        if marker["generation"] != package.generation or expected["accounts"] != accounts:
            raise SandboxInitializationError("Broker credential generation is invalid")
    return {**marker, "config_digest": digest}


def read_ready_marker(path: Path, *, expected_proxy_port: int | None = None) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SandboxInitializationError("Broker ready marker is unavailable") from exc
    return validate_ready_marker(value, expected_proxy_port=expected_proxy_port)


def write_ready_marker(path: Path, marker: Mapping[str, object]) -> None:
    validated = validate_ready_marker(marker)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(validated, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def secrets_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


__all__ = [
    "READY_SCHEMA",
    "TOKEN_MODEL",
    "build_ready_marker",
    "read_ready_marker",
    "validate_ready_marker",
    "write_ready_marker",
]
