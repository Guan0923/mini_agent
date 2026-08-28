"""Local Windows Sandbox Broker control-plane routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

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
            content={"detail": exc.safe_message, "code": exc.broker_code.value},
        )
    except Exception as exc:
        logger.warning("sandbox broker install failed code=%s", type(exc).__name__, exc_info=False)
        return JSONResponse(
            status_code=503,
            content={"detail": "沙箱 Broker 安装失败，请查看后端日志。", "code": "broker_install_failed"},
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
            content={"detail": exc.safe_message, "code": exc.broker_code.value},
        )
    except Exception as exc:
        logger.warning("sandbox broker repair failed code=%s", type(exc).__name__, exc_info=False)
        return JSONResponse(
            status_code=503,
            content={"detail": "沙箱 Broker 修复失败，请查看后端日志。", "code": "broker_install_failed"},
        )


__all__ = ["router"]


def _broker_payload(value: object) -> dict[str, object]:
    if callable(getattr(value, "to_dict", None)):
        return dict(value.to_dict())
    if isinstance(value, dict):
        return dict(value)
    return {"installed": False, "healthy": False, "detail": "Broker returned an invalid status"}
