"""Local Windows Sandbox Broker control-plane routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.domain import safe_error_message
from backend.sandbox import (
    BrokerConfiguration,
    SandboxMaintenanceBusy,
    WindowsBrokerClient,
)
from backend.sandbox.errors import BrokerInstallationError

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])
logger = logging.getLogger(__name__)


def _broker(request: Request):
    broker = getattr(request.app.state.web, "sandbox_broker", None)
    if broker is None or not callable(getattr(broker, "status", None)):
        raise HTTPException(status_code=503, detail="沙箱 Broker 尚未初始化。")
    return broker


@router.get("/status")
def status(request: Request) -> dict[str, object]:
    return _broker_payload(_broker(request).status())


@router.post("/install", response_model=None)
def install(request: Request) -> dict[str, object] | JSONResponse:
    try:
        return _broker_payload(_broker(request).install())
    except BrokerInstallationError as exc:
        logger.warning("sandbox broker install failed code=%s", exc.broker_code.value, exc_info=False)
        return JSONResponse(
            status_code=503,
            content={"detail": safe_error_message(exc), "code": exc.broker_code.value},
        )
    except Exception as exc:
        logger.warning("sandbox broker install failed code=%s", type(exc).__name__, exc_info=False)
        return JSONResponse(
            status_code=503,
            content={"detail": safe_error_message(exc), "code": "broker_install_failed"},
        )


@router.post("/repair", response_model=None)
def repair(request: Request) -> dict[str, object] | JSONResponse:
    try:
        broker = _broker(request)
        current = _broker_payload(broker.status())
        operation = broker.install if current.get("installed") is not True else broker.repair
        return _broker_payload(operation())
    except BrokerInstallationError as exc:
        logger.warning("sandbox broker repair failed code=%s", exc.broker_code.value, exc_info=False)
        return JSONResponse(
            status_code=503,
            content={"detail": safe_error_message(exc), "code": exc.broker_code.value},
        )
    except Exception as exc:
        logger.warning("sandbox broker repair failed code=%s", type(exc).__name__, exc_info=False)
        return JSONResponse(
            status_code=503,
            content={"detail": safe_error_message(exc), "code": "broker_install_failed"},
        )


@router.post("/reinstall", response_model=None)
def reinstall(request: Request) -> dict[str, object] | JSONResponse:
    state = request.app.state.web
    gate = getattr(state, "sandbox_maintenance", None)
    try:
        if gate is None or not callable(getattr(gate, "acquire_maintenance", None)):
            raise SandboxMaintenanceBusy("Sandbox maintenance gate is unavailable")
        with gate.acquire_maintenance():
            broker = _broker(request)
            manifest_path = Path(getattr(state, "sandbox_manifest_path", BrokerConfiguration.create().manifest_path))
            if _manifest_has_records(manifest_path):
                try:
                    broker.reclaim_stale()
                except Exception:
                    return _jobs_active_response("无法确认沙箱任务已全部清理，请停止运行中的 Turn 后重试。")
                if _manifest_has_records(manifest_path):
                    return _jobs_active_response("仍有沙箱命令资源正在使用，无法重装 Broker。")
            result = broker.reinstall()
            lease_path = Path(state.paths.runtime_dir) / "sandbox-leases.json"
            lease_path.unlink(missing_ok=True)
            if isinstance(broker, WindowsBrokerClient):
                proxy_port = int(state.settings.sandbox_config()["proxy_port"])
                state.sandbox_broker = WindowsBrokerClient.from_system(expected_proxy_port=proxy_port)
                refreshed = state.sandbox_broker.status()
                return _broker_payload(refreshed if refreshed.healthy else result)
            return _broker_payload(result)
    except SandboxMaintenanceBusy:
        return _jobs_active_response("仍有沙箱命令正在运行或等待启动，无法重装 Broker。")
    except BrokerInstallationError as exc:
        logger.warning("sandbox broker reinstall failed code=%s", exc.broker_code.value, exc_info=False)
        return JSONResponse(
            status_code=503,
            content={"detail": safe_error_message(exc), "code": exc.broker_code.value},
        )
    except Exception as exc:
        logger.warning("sandbox broker reinstall failed code=%s", type(exc).__name__, exc_info=False)
        return JSONResponse(
            status_code=503,
            content={"detail": safe_error_message(exc), "code": "broker_install_failed"},
        )


__all__ = ["router"]


def _broker_payload(value: object) -> dict[str, object]:
    if callable(getattr(value, "to_dict", None)):
        return dict(value.to_dict())
    if isinstance(value, dict):
        return dict(value)
    return {
        "installed": False,
        "healthy": False,
        "code": "broker_status_failed",
        "detail": "Broker returned an invalid status",
    }


def _jobs_active_response(detail: str) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": detail, "code": "broker_jobs_active"})


def _manifest_has_records(path: Path) -> bool:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    if not isinstance(raw, dict) or not isinstance(raw.get("records"), list):
        raise ValueError("Broker resource manifest is invalid")
    return bool(raw["records"])
