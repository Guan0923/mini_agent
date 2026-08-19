"""Authenticated Windows Sandbox Broker control-plane endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .auth.routes import _broker_payload, _origin_guard

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


def _broker(request: Request):
    broker = getattr(request.app.state.web, "sandbox_broker", None)
    if broker is None or not callable(getattr(broker, "status", None)):
        raise HTTPException(status_code=503, detail="沙箱 Broker 尚未初始化。")
    return broker


@router.get("/status")
def status(request: Request) -> dict[str, object]:
    return _broker_payload(_broker(request).status())


@router.post("/install")
def install(request: Request) -> dict[str, object]:
    _origin_guard(request)
    try:
        return _broker_payload(_broker(request).install())
    except Exception as exc:
        raise HTTPException(status_code=503, detail="沙箱 Broker 安装失败。") from exc


@router.post("/repair")
def repair(request: Request) -> dict[str, object]:
    _origin_guard(request)
    try:
        return _broker_payload(_broker(request).repair())
    except Exception as exc:
        raise HTTPException(status_code=503, detail="沙箱 Broker 修复失败。") from exc


__all__ = ["router"]
