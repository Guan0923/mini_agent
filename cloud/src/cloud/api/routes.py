"""Versioned cloud API routes."""

from __future__ import annotations

import base64
import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cloud.auth.mail import MailDeliveryError
from cloud.auth.types import AuthError, AuthStorageUnavailable, RateLimitError, UserIdentity
from cloud.sync.repository import CloudSyncConflict, EncryptedSnapshotChunk


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class RegisterRequest(EmailRequest):
    code: str = Field(min_length=6, max_length=6)
    password: str = Field(min_length=1, max_length=128)


class LoginRequest(EmailRequest):
    password: str = Field(min_length=1, max_length=128)


class DeviceStartRequest(BaseModel):
    server_url: str = Field(default="", max_length=2000)


class DevicePollRequest(BaseModel):
    poll_secret: str = Field(min_length=20, max_length=256)


class DeviceApproveRequest(BaseModel):
    grant: str = Field(min_length=20, max_length=256)
    approved: bool


class KeyRequest(BaseModel):
    dek: str = Field(min_length=32, max_length=256)


class SnapshotBeginRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=160)
    parent_snapshot_id: str | None = Field(default=None, max_length=160)
    local_revision: int = Field(ge=0)
    device_id: str = Field(min_length=1, max_length=160)
    force: bool = False


class SnapshotChunkRequest(BaseModel):
    nonce: str = Field(min_length=1, max_length=128)
    ciphertext: str = Field(min_length=1, max_length=2_000_000)
    checksum: str = Field(min_length=64, max_length=128)


class SnapshotCompleteRequest(BaseModel):
    archive_sha256: str = Field(min_length=64, max_length=128)
    archive_size: int = Field(ge=1)
    chunk_count: int = Field(ge=1)


def _state(request: Request):
    return request.app.state.cloud


def _identity(value: UserIdentity) -> dict[str, object]:
    return {"id": value.id, "email": value.email, "kind": value.kind}


def _token_from_header(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。")
    return token


def current_identity(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> UserIdentity:
    token = _token_from_header(authorization)
    resolved = _state(request).auth_service.resolve_token(token)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已过期，请重新登录。")
    identity, kind = resolved
    request.state.cloud_token = token
    request.state.cloud_auth_kind = kind
    return identity


def _auth_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RateLimitError):
        return HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)})
    if isinstance(exc, MailDeliveryError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AuthStorageUnavailable):
        return HTTPException(status_code=503, detail="云端认证数据库暂不可用。")
    if isinstance(exc, AuthError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="云端服务暂时无法处理请求。")


def build_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["cloud"])

    @router.post("/auth/register/code", status_code=202)
    def register_code(body: EmailRequest, request: Request) -> dict[str, str]:
        try:
            _state(request).auth_service.request_code(
                body.email, "register", request.client.host if request.client else None
            )
        except Exception as exc:
            raise _auth_error(exc) from exc
        return {"detail": "如果邮箱可用，验证码已发送。"}

    @router.post("/auth/register")
    def register(body: RegisterRequest, request: Request) -> dict[str, object]:
        try:
            identity, token = _state(request).auth_service.register(body.email, body.code, body.password)
        except Exception as exc:
            raise _auth_error(exc) from exc
        return {"user": _identity(identity), "access_token": token, "token_type": "Bearer", "expires_in": 2_592_000}

    @router.post("/auth/login")
    def login(body: LoginRequest, request: Request) -> dict[str, object]:
        try:
            identity, token = _state(request).auth_service.login(
                body.email, body.password, request.client.host if request.client else None
            )
        except Exception as exc:
            raise _auth_error(exc) from exc
        return {"user": _identity(identity), "access_token": token, "token_type": "Bearer", "expires_in": 2_592_000}

    @router.post("/auth/password-reset/code", status_code=202)
    def reset_code(body: EmailRequest, request: Request) -> dict[str, str]:
        try:
            _state(request).auth_service.request_code(
                body.email, "reset", request.client.host if request.client else None
            )
        except Exception as exc:
            raise _auth_error(exc) from exc
        return {"detail": "如果邮箱已注册，验证码已发送。"}

    @router.post("/auth/password-reset/confirm")
    def reset_password(body: RegisterRequest, request: Request) -> dict[str, object]:
        try:
            identity, token = _state(request).auth_service.reset_password(body.email, body.code, body.password)
        except Exception as exc:
            raise _auth_error(exc) from exc
        return {"user": _identity(identity), "access_token": token, "token_type": "Bearer", "expires_in": 2_592_000}

    @router.get("/auth/me")
    def me(identity: Annotated[UserIdentity, Depends(current_identity)]) -> dict[str, object]:
        return {"user": _identity(identity)}

    @router.post("/auth/logout")
    def logout(request: Request, identity: Annotated[UserIdentity, Depends(current_identity)]) -> dict[str, str]:
        del identity
        _state(request).auth.revoke_token(request.state.cloud_token)
        return {"detail": "已退出登录。"}

    @router.post("/devices/start")
    def device_start(body: DeviceStartRequest, request: Request) -> dict[str, object]:
        poll, browser, expires = _state(request).auth_service.start_device(body.server_url)
        return {
            "poll_secret": poll,
            "verification_url": _state(request).auth_service.device_url(browser),
            "expires_in": expires,
            "poll_interval": 2,
        }

    @router.get("/devices/info")
    def device_info(grant: str, request: Request) -> dict[str, object]:
        value = _state(request).auth.device_info(grant)
        if value is None:
            raise HTTPException(status_code=404, detail="设备授权请求不存在或已过期。")
        return value

    @router.post("/devices/approve")
    def device_approve(
        body: DeviceApproveRequest,
        request: Request,
        identity: Annotated[UserIdentity, Depends(current_identity)],
    ) -> dict[str, str]:
        accepted = _state(request).auth.approve_device(body.grant, identity.id, body.approved)
        if not accepted:
            raise HTTPException(status_code=410, detail="设备授权请求不存在、已处理或已过期。")
        return {"status": "approved" if body.approved else "denied"}

    @router.post("/devices/token")
    def device_token(body: DevicePollRequest, request: Request) -> JSONResponse:
        status_name, token = _state(request).auth.poll_device(body.poll_secret)
        if status_name == "pending":
            return JSONResponse({"status": "authorization_pending"}, status_code=202)
        if status_name == "approved" and token:
            return JSONResponse(
                {"status": "approved", "access_token": token, "token_type": "Bearer", "expires_in": 2_592_000}
            )
        if status_name == "denied":
            return JSONResponse({"status": "access_denied"}, status_code=403)
        if status_name == "expired":
            return JSONResponse({"status": "expired"}, status_code=410)
        return JSONResponse({"status": "invalid_grant"}, status_code=400)

    @router.get("/sync/snapshots")
    def list_snapshots(
        request: Request,
        identity: Annotated[UserIdentity, Depends(current_identity)],
    ) -> list[dict[str, object]]:
        return _state(request).snapshots.list_snapshots(identity.id)

    @router.post("/sync/keys")
    @router.post("/sync/keys/ensure")
    def ensure_key(
        body: KeyRequest, request: Request, identity: Annotated[UserIdentity, Depends(current_identity)]
    ) -> dict[str, bool]:
        try:
            dek = base64.b64decode(body.dek.encode("ascii"), altchars=b"-_", validate=True)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(status_code=422, detail="数据密钥格式无效。") from exc
        if len(dek) != 32:
            raise HTTPException(status_code=422, detail="数据密钥长度无效。")
        try:
            _state(request).snapshots.ensure_user_key(identity.id, dek)
        except CloudSyncConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"stored": True}

    @router.get("/sync/keys")
    @router.get("/sync/keys/recover")
    def recover_key(
        request: Request, identity: Annotated[UserIdentity, Depends(current_identity)]
    ) -> dict[str, str | None]:
        key = _state(request).snapshots.recover_user_key(identity.id)
        return {"dek": base64.urlsafe_b64encode(key).decode("ascii") if key is not None else None}

    @router.post("/sync/snapshots/begin")
    def begin_snapshot(
        body: SnapshotBeginRequest,
        request: Request,
        identity: Annotated[UserIdentity, Depends(current_identity)],
    ) -> dict[str, int]:
        try:
            version = _state(request).snapshots.begin_snapshot(
                snapshot_id=body.snapshot_id,
                user_id=identity.id,
                parent_snapshot_id=body.parent_snapshot_id,
                local_revision=body.local_revision,
                device_id=body.device_id,
                force=body.force,
            )
        except CloudSyncConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"version": version}

    @router.put("/sync/snapshots/{snapshot_id}/chunks/{sequence}")
    def append_chunk(
        snapshot_id: str,
        sequence: int,
        body: SnapshotChunkRequest,
        request: Request,
        identity: Annotated[UserIdentity, Depends(current_identity)],
    ) -> dict[str, object]:
        if sequence < 0:
            raise HTTPException(status_code=422, detail="快照分块序号无效。")
        try:
            nonce = base64.b64decode(body.nonce.encode("ascii"), altchars=b"-_", validate=True)
            ciphertext = base64.b64decode(body.ciphertext.encode("ascii"), altchars=b"-_", validate=True)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(status_code=422, detail="快照分块格式无效。") from exc
        if len(nonce) != 12 or len(ciphertext) < 16:
            raise HTTPException(status_code=422, detail="快照分块格式无效。")
        if body.checksum != hashlib.sha256(ciphertext).hexdigest():
            raise HTTPException(status_code=422, detail="快照分块校验和无效。")
        chunk = EncryptedSnapshotChunk(sequence, nonce, ciphertext, body.checksum)
        try:
            _state(request).snapshots.append_chunk(identity.id, snapshot_id, chunk)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"accepted": True, "sequence": sequence}

    @router.post("/sync/snapshots/{snapshot_id}/complete")
    def complete_snapshot(
        snapshot_id: str,
        body: SnapshotCompleteRequest,
        request: Request,
        identity: Annotated[UserIdentity, Depends(current_identity)],
    ) -> dict[str, bool]:
        try:
            _state(request).snapshots.complete_snapshot(
                identity.id,
                snapshot_id,
                archive_sha256=body.archive_sha256,
                archive_size=body.archive_size,
                chunk_count=body.chunk_count,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"completed": True}

    @router.post("/sync/snapshots/{snapshot_id}/fail")
    def fail_snapshot(
        snapshot_id: str, request: Request, identity: Annotated[UserIdentity, Depends(current_identity)]
    ) -> dict[str, bool]:
        _state(request).snapshots.fail_snapshot(identity.id, snapshot_id)
        return {"failed": True}

    @router.get("/sync/snapshots/{snapshot_id}")
    def download_snapshot(
        snapshot_id: str,
        request: Request,
        identity: Annotated[UserIdentity, Depends(current_identity)],
    ) -> dict[str, object]:
        try:
            metadata, chunks = _state(request).snapshots.download(identity.id, snapshot_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "metadata": metadata,
            "chunks": [
                {
                    "sequence": item.sequence,
                    "nonce": base64.urlsafe_b64encode(item.nonce).decode("ascii"),
                    "ciphertext": base64.urlsafe_b64encode(item.ciphertext).decode("ascii"),
                    "checksum": item.checksum,
                }
                for item in chunks
            ],
        }

    return router


__all__ = ["build_router", "current_identity"]
