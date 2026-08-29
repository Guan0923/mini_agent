"""Session file upload, search, content preview, and deletion endpoints.

All routes are authenticated and session-scoped.  Project roots come from the
session's effective cwd (``WebAppState.session_workspace``), so an external
project folder is searchable while uploads always live inside the session's
own workspace.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from ..auth.dependencies import require_user
from ..auth.types import UserIdentity
from ..session_store import require_active_session, session_store
from ..state import WebAppState
from .store import SessionFileError, SessionFileStore

router = APIRouter(prefix="/api")

_CONTENT_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"})


def _store_for(state: WebAppState, identity: UserIdentity, session_id: str) -> SessionFileStore:
    """Build the session file store with the validated project root."""

    store = session_store(state, identity.id)
    require_active_session(store, session_id)
    paths = state.user_paths(identity.id)
    paths.ensure_session(session_id)
    project_root = None
    try:
        project_root = state.session_workspace(identity.id, session_id)
    except RuntimeError:
        # A removed/unavailable project cwd keeps uploads usable while
        # project-root search and content resolve to nothing.
        project_root = None
    return SessionFileStore(paths, session_id, project_root=project_root)


def _file_error(exc: SessionFileError) -> HTTPException:
    if "不存在" in str(exc) or "无效" in str(exc):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/sessions/{session_id}/files")
def upload_session_files(
    session_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    identity: UserIdentity = Depends(require_user),
) -> list[dict[str, object]]:
    """Upload a bounded multipart batch; every file lands immediately."""

    state: WebAppState = request.app.state.web
    try:
        store = _store_for(state, identity, session_id)
        items = [(upload.filename, upload.file) for upload in files]
        return store.store_batch(items)
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.get("/sessions/{session_id}/files")
def search_session_files(
    session_id: str,
    request: Request,
    q: str = "",
    limit: int = 20,
    identity: UserIdentity = Depends(require_user),
) -> list[dict[str, object]]:
    state: WebAppState = request.app.state.web
    try:
        store = _store_for(state, identity, session_id)
        return store.search(q, limit)
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.get("/sessions/{session_id}/files/content")
def session_file_content(
    session_id: str,
    request: Request,
    source: str,
    path: str,
    download: bool = False,
    identity: UserIdentity = Depends(require_user),
) -> Response:
    """Return an authenticated preview (inline) or download (attachment)."""

    state: WebAppState = request.app.state.web
    try:
        store = _store_for(state, identity, session_id)
        resolved = store.resolve(source, path)
        mime = _mime_type(resolved.name)
        is_image = _is_image_file(resolved.name, mime)
        disposition = "attachment" if download or not is_image else "inline"
        response = FileResponse(
            resolved,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'{disposition}; filename="{_safe_filename(resolved.name)}"',
                "Cache-Control": "private, no-store",
            },
        )
        return response
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.head("/sessions/{session_id}/files/content")
def session_file_content_head(
    session_id: str,
    request: Request,
    source: str,
    path: str,
    download: bool = False,
    identity: UserIdentity = Depends(require_user),
) -> Response:
    """Validate an authenticated file reference without returning its body."""

    state: WebAppState = request.app.state.web
    try:
        store = _store_for(state, identity, session_id)
        resolved = store.resolve(source, path)
        mime = _mime_type(resolved.name)
        is_image = _is_image_file(resolved.name, mime)
        disposition = "attachment" if download or not is_image else "inline"
        return Response(
            status_code=200,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'{disposition}; filename="{_safe_filename(resolved.name)}"',
                "Cache-Control": "private, no-store",
            },
        )
    except SessionFileError as exc:
        raise _file_error(exc) from exc


@router.delete("/sessions/{session_id}/files")
def delete_session_file(
    session_id: str,
    request: Request,
    source: str,
    path: str,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, str]:
    """Delete one session-uploaded file; project files are never deletable."""

    state: WebAppState = request.app.state.web
    if source != "upload":
        raise HTTPException(status_code=403, detail="只能删除会话上传的文件。")
    try:
        store = _store_for(state, identity, session_id)
        store.delete_upload(path)
    except SessionFileError as exc:
        raise _file_error(exc) from exc
    return {"deleted": path}


def _mime_type(name: str) -> str:
    import mimetypes

    guessed, _encoding = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _is_image_file(name: str, mime: str) -> bool:
    return name.rsplit(".", 1)[-1].casefold() in _CONTENT_IMAGE_EXTENSIONS or mime.startswith("image/")


def _safe_filename(name: str) -> str:
    return name.replace('"', "_").replace("\\", "_").replace("/", "_")


__all__ = ["router"]
