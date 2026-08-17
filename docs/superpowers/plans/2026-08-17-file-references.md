# Web File Upload & @-References Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add session-scoped file upload (button/drag/paste), `@` file completion in the Web composer, and structured `references` metadata that flows through chat requests, RuntimeState nodes, history projection, branching/rewind, and sync snapshots — without injecting file contents into model context.

**Architecture:** Backend adds a session-file store service (upload/search/content/delete) under `backend/src/api/session_files/`, migrates the uploads layout from `runtime/<sid>/uploads` to `runtime/<sid>/workspace/uploads`, extends `ChatRequest` with validated `references`, persists references as user-message metadata in RuntimeState nodes, and registers a read-only `read_upload_file` tool for the agent. Frontend adds `frontend/src/api/files.ts`, an `@`-completion + upload strip in `Composer.tsx` driven from `ChatPage.tsx`, and reference chips on user messages.

**Tech Stack:** Python 3.11+ / FastAPI / pydantic / SQLite (RuntimeState nodes) / React 18 / antd v6 (Upload customRequest, pastable) / TypeScript.

## Global Constraints

- Uploads land immediately at `~/.mini_agent/<user_id>/runtime/<session_id>/workspace/uploads/`.
- Upload batch: max 20 files, 50 MiB per file, 200 MiB total per request. Arbitrary formats allowed.
- Reject path traversal, symlinks, special files. Names sanitized; conflicts become `name (2).ext`.
- Clipboard images: `image-YYYYMMDD-HHmmss-<8 hex chars>.<ext>` (client-generated name, server sanitizes).
- References: max 50 per request; `{source: "project"|"upload", path}`; server revalidates ownership + existence; persisted as `data.message.references` on the user node.
- Structured references NEVER pass through `FileReferenceExpander`; legacy `@path` behavior remains when no structured references are submitted.
- Old layout `runtime/<sid>/uploads` migrates to `workspace/uploads`; sync/branch/rewind/guest-import copy only the new canonical dir; old snapshot restore auto-migrates.
- Response item shape: `{source, path, name, size, mime, mtime, is_image}`.
- All chat/history/edit/branch/rewind paths carry reference metadata; invalid files render as "文件不可用" instead of silently re-resolving.

---

### Task 1: Uploads layout migration (ClientPaths + user_data + sync)

**Files:**
- Modify: `backend/src/configuration.py:160-186` (`session_uploads`, `ensure_session`)
- Modify: `backend/src/api/user_data.py:114-164` (`copy_session_files`, `copy_session_uploads`), `:241-267` (`_copy_session_payload`)
- Modify: `backend/src/runtime/conversation/service.py:256-283` (`_start_isolated_handoff_session`)
- Modify: `backend/src/sync/snapshots.py:376-391` (`_materialize`), `:646-649` (restore)
- Test: `tests/test_web_user_data.py`

**Interfaces:**
- Consumes: existing `ClientPaths` layout.
- Produces: `ClientPaths.session_uploads(session_id)` now returns `session_root/workspace/uploads`; new `ClientPaths.legacy_session_uploads(session_id)` returns `session_root/uploads`; `ensure_session` migrates legacy → canonical.

- [ ] **Step 1: Write the failing migration test**

```python
# tests/test_web_user_data.py (append)
def test_legacy_uploads_migrate_into_workspace(tmp_path: Path) -> None:
    paths = ClientPaths(user_root(tmp_path, USER_ID))
    root = paths.session_root("session_migrate")
    root.mkdir(parents=True)
    (root / "workspace").mkdir()
    legacy = root / "uploads"
    legacy.mkdir()
    (legacy / "old.txt").write_text("legacy", encoding="utf-8")
    paths.ensure_session("session_migrate")
    assert (paths.session_uploads("session_migrate") / "old.txt").read_text(encoding="utf-8") == "legacy"
    assert not (root / "uploads").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_web_user_data.py::test_legacy_uploads_migrate_into_workspace -v`
Expected: FAIL (session_uploads still legacy path / no migration).

- [ ] **Step 3: Implement layout migration**

In `configuration.py`: `session_uploads(session_id)` returns `self.session_root(session_id) / "workspace" / "uploads"`; add `legacy_session_uploads`; in `ensure_session`, after creating dirs, call `self._migrate_legacy_uploads(session_id)` which moves legacy children into canonical (rejecting symlinks) and removes the legacy dir when empty. In `user_data.py`, update `copy_session_files` to copy only the workspace tree (uploads inside), `copy_session_uploads` to copy `session_uploads` only, and `_copy_session_payload` to copy only `workspace`. In `service.py`, drop the separate uploads copy (workspace copy covers it). In `snapshots.py` `_materialize`, copy only `workspace` and index roots `{"workspace": ...}`; restore keeps `ensure_session` (now migrates legacy from old snapshots).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_web_user_data.py tests/test_cloud_snapshots.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/configuration.py backend/src/api/user_data.py backend/src/runtime/conversation/service.py backend/src/sync/snapshots.py tests/test_web_user_data.py
git commit -m "feat: migrate session uploads into workspace/uploads"
```

---

### Task 2: Session file store service

**Files:**
- Create: `backend/src/api/session_files/store.py`
- Test: `tests/test_session_files.py`

**Interfaces:**
- Consumes: `ClientPaths`, `WebAppState.session_workspace`, `WorkspaceFiles` ignore policy.
- Produces: `SessionFileStore(paths, session_workspace)` with:
  - `upload_root() -> Path`
  - `project_root() -> Path`
  - `sanitize_name(name: str) -> str`
  - `unique_target(upload_root, name) -> tuple[Path, str]`
  - `store_batch(files: list[UploadFile]) -> list[dict]` (streaming, atomic)
  - `search(q: str, limit: int) -> list[dict]`
  - `resolve(source, path) -> Path` (raises `SessionFileError`)
  - `metadata(path, source) -> dict`
  - `delete_upload(path) -> None`
- `SessionFileError(ValueError)` with Chinese messages.

- [ ] **Step 1: Write failing tests** (isolation, binary integrity, naming, conflicts, limits, traversal, symlinks, search sources, delete permission)

```python
# tests/test_session_files.py
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.sessions.routes import _store
from backend.api.state import WebAppState
from backend.storage.auth import LocalAuthStore
from backend.storage.user_settings import PerUserSettingsRepository

USER_ID = "123e4567-e89b-12d3-a456-426614174000"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(WebAppState(tmp_path, auth_repository=LocalAuthStore(tmp_path / "auth.db")))
    app.state.web.auth.create_user(USER_ID, "user@example.com", "secret")
    test_client = TestClient(app)
    summary = test_client.post("/api/sessions", headers={"X-User-Id": USER_ID}, json={"title": "t"}).json()
    test_client.session_id = summary["session_id"]
    test_client.user_id = USER_ID
    return test_client
```

(The auth header contract must be verified against `backend/src/api/auth/dependencies.py`; adjust fixture to the real auth flow used by `tests/test_web_auth.py`.) Cover: upload → list roundtrip; binary equality; `name (2).ext` conflicts; >20 files rejected; >50 MiB rejected; traversal `../x` rejected; symlink file rejected; search returns `source` both roots; DELETE removes upload file but not project file.

- [ ] **Step 2: Implement `store.py`** (streaming chunked writes to temp files, `os.replace` batch rename, `mimetypes` + image extension set, bounded `os.walk` reusing `WorkspaceFiles._IGNORED_DIRECTORIES`, path confinement via `workspace_relative_parts`).

- [ ] **Step 3: Run tests**, fix, commit `feat: add session file store`.

---

### Task 3: Session files API routes

**Files:**
- Create: `backend/src/api/session_files/routes.py`
- Modify: `backend/src/api/app.py:45-58` (register router)
- Modify: `backend/src/api/__init__.py` exports
- Test: extend `tests/test_session_files.py`

**Interfaces:**
- `POST /api/sessions/{session_id}/files` (multipart `files`), `GET /api/sessions/{session_id}/files?q=&limit=`, `GET /api/sessions/{session_id}/files/content?source=&path=&download=`, `DELETE /api/sessions/{session_id}/files?source=&path=`.
- Content responses: `FileResponse` with `X-Content-Type-Options: nosniff`, `Content-Disposition: inline` for images / `attachment` otherwise; `Cache-Control: private, no-store`.

- [ ] **Step 1: Write failing route tests** (401/404 for foreign sessions, preview headers, download disposition, delete permission).
- [ ] **Step 2: Implement routes** using `_store`, `_require_active`, `state.session_workspace`, `SessionFileStore`; guard `session_id` via `_require_summary`; never normalize session ids into paths before validation.
- [ ] **Step 3: Register router** in `app.py` with `Depends(require_user)`.
- [ ] **Step 4: Run tests**, commit `feat: add session file upload/search/content/delete APIs`.

---

### Task 4: Chat references (request model + validation + persistence)

**Files:**
- Modify: `backend/src/api/chat/routes.py` (`ChatRequest.references`, `_stream`, `chat` route, `_validate_references`)
- Modify: `backend/src/runtime/node_bridge.py:60-77,342-352` (`references` param; user node payload)
- Modify: `backend/src/runtime/conversation/service.py:162-224` (`run_task` accepts `references`; skip expander)
- Modify: `backend/src/runtime/conversation/references.py` (`FileReferenceExpander.expand(task, structured=False)`)
- Test: extend `tests/test_web_api.py`, `tests/test_web_chat.py`

**Interfaces:**
- `FileReference` pydantic model: `source: Literal["project","upload"]`, `path: str` (max_length 2000).
- `ChatRequest.references: list[FileReference] = Field(default_factory=list, max_length=50)`.
- `ConversationService.run_task(..., references: Sequence[Mapping[str,str]] = ())`; `_prepare(task, structured=bool(references))`.
- `RuntimeEventNodeBridge(..., references: Sequence[Mapping[str, str]] | None = None)`; user node `message_payload("user", prompt, **({"references": list(references)} if references else {}))`.

- [ ] **Step 1: Write failing tests** (ChatRequest accepts references; references persisted on user node via bridge.start(); expander skipped when structured refs present; >50 rejected; invalid source rejected).
- [ ] **Step 2: Implement** validation in the `chat` route: resolve each `project` ref inside `state.session_workspace`, each `upload` ref inside `paths.session_uploads`; missing/cross-session refs → 422. Pass to `_stream` → `run_task` → bridge.
- [ ] **Step 3: Run tests**, commit `feat: validate and persist chat file references`.

---

### Task 5: Read-only upload-file tool

**Files:**
- Modify: `backend/src/tools/default_tools/filesystem.py` (add `upload_file_read_tool(files)`)
- Modify: `backend/src/tools/default_tools/__init__.py:18-35`
- Modify: `backend/src/runtime/application/factory.py:41-102` (`build_application(upload_root=...)`), `:127-189`
- Modify: `backend/src/api/shared/runtime.py:21-101` (pass `upload_root`)
- Test: `tests/test_tools.py`, `tests/test_web_chat.py`

**Interfaces:**
- `Tool("read_upload_file", ...)` — `WorkspaceFiles(upload_root).read_file` with `path`, `start_line`, `max_lines`, `start_column`; read-only, no confirmation.
- `build_application(..., upload_root: Path | None = None)`; registered via `build_tool_registry(..., extra_tools=(upload_file_tool,))` when set.
- `build_user_application` computes `upload_root = paths.session_uploads(session_id)` (after `paths.ensure_session(session_id)`).

- [ ] **Step 1: Write failing test** — build app with `upload_root`, assert `read_upload_file` in registry and reads a file inside uploads; rejects `../escape`.
- [ ] **Step 2: Implement** tool + factory threading.
- [ ] **Step 3: Run tests**, commit `feat: add read-only upload file tool`.

---

### Task 6: Projection, history, run controller (backend + frontend types)

**Files:**
- Modify: `backend/src/api/sessions/projection.py:137-149` (user payload includes `references`)
- Modify: `frontend/src/types.ts` (`FileReference`, `ChatMessage.references`, `SessionMessage` in api)
- Modify: `frontend/src/app/storage.ts:127-140` (`transcriptToMessages`)
- Modify: `frontend/src/app/runtimeDetailProjection.ts` (`projectRuntimeNode` user branch + `integrateRuntimeNodeFrame`)
- Modify: `frontend/src/api/sessions.ts` (`SessionMessage.references`)
- Modify: `frontend/src/api/chat.ts` (`StreamOptions.references`, body)
- Modify: `frontend/src/app/types.ts` + `frontend/src/app/runController.ts` (`ChatRunRequest.references` → `streamChat`)
- Modify: `frontend/src/pages/chat/ChatPage.tsx` (`runStream`, `runPrompt`, `send`, edit/rewind restore references)
- Test: `tests/test_transcript.py`, `frontend/src/app/runtimeDetailProjection.test.ts`, `frontend/src/pages/ChatPage.test.tsx`

- [ ] **Step 1: Backend projection test** — node with `message.references` projects into transcript user entry.
- [ ] **Step 2: Frontend mapping tests** — `transcriptToMessages` copies `references`; `integrateRuntimeNodeFrame` user frame sets `references`.
- [ ] **Step 3: Implement** all mappings + run-controller pass-through.
- [ ] **Step 4: Run tests**, commit `feat: carry file references through history and runs`.

---

### Task 7: Frontend API client for session files

**Files:**
- Create: `frontend/src/api/files.ts`
- Modify: `frontend/src/api/index.ts`
- Test: `frontend/src/api.test.ts` (pattern-consistent)

**Interfaces:**
- `uploadSessionFiles(sessionId, files: File[], onProgress?: (percent) => void): Promise<SessionFileInfo[]>`
- `searchSessionFiles(sessionId, q: string, limit = 20): Promise<SessionFileInfo[]>`
- `sessionFileContentUrl(sessionId, source, path, download = false): string`
- `deleteSessionFile(sessionId, source, path): Promise<void>`
- `SessionFileInfo {source, path, name, size, mime, mtime, is_image}`

- [ ] **Step 1: Implement `files.ts`** (XHR upload with progress for customRequest compatibility; `apiUrl`).
- [ ] **Step 2: Export** from index; run `tsc --noEmit`; commit `feat: add frontend session file API`.

---

### Task 8: Composer `@`-completion

**Files:**
- Create: `frontend/src/commands/fileCompletion.ts` (pure helpers)
- Modify: `frontend/src/pages/chat/Composer.tsx` (menu + props)
- Modify: `frontend/src/pages/chat/ChatPage.tsx` (caret tracking, search debounce, completion state, mutual exclusion with `/` menu)
- Modify: `frontend/src/commands/completion.ts` (`commandKeyAction` extension or new `fileKeyAction`)
- Modify: `frontend/src/styles/composer.css`
- Test: `frontend/src/commands/fileCompletion.test.ts`, `frontend/src/pages/ChatPage.test.tsx`

**Interfaces (pure helpers in `fileCompletion.ts`):**
- `fileTrigger(input: string, caret: number): { query: string; start: number } | null` — `@` at start or after whitespace.
- `completionToken(query, path: string): string` — `@path` or `@"path with space"`.
- `insertToken(input, start, caret, token): { value: string; caret: number }`.
- `fileKeyAction({key, shiftKey, isComposing, menuVisible})` — mirrors `commandKeyAction`.

- [ ] **Step 1: Write pure-helper tests** (trigger at start / after space / mid-word no; token quoting; insert keeps rest; IME guard).
- [ ] **Step 2: Implement helpers.**
- [ ] **Step 3: Wire ChatPage** — track `taRef` selection; debounce search (250 ms) on trigger; menu state `{query, results, activeIndex, start}`; completion inserts token + adds `{source, path}` to `references` state; Esc/Enter/Tab/arrows handled in `handleComposerKeyDown`; file menu takes precedence over `/` menu; `commandMenuVisible` only when no file menu.
- [ ] **Step 4: Style menu** above input (`.file-menu`, `.file-item`, source badge 项目文件/会话上传, selected state).
- [ ] **Step 5: Component tests** (type ` @re` → menu; arrow+Enter inserts `@readme.md`; space-path quoting; Esc dismiss; IME composing does not complete).
- [ ] **Step 6: Run frontend tests + typecheck**, commit `feat: add at-file completion to composer`.

---

### Task 9: Upload UI (button, drag, paste, strip)

**Files:**
- Modify: `frontend/src/pages/chat/Composer.tsx` (Upload, pending strip)
- Modify: `frontend/src/pages/chat/ChatPage.tsx` (upload state, delete handling, auto-insert)
- Modify: `frontend/src/styles/composer.css`
- Test: `frontend/src/pages/ChatPage.test.tsx`

**Interfaces:**
- `ChatPage` state: `pendingUploads: PendingUpload[]` `{uid, name, path?, source, status: "uploading"|"done"|"error", percent, isImage}`; `references: FileReference[]`.
- Send: `references` included in `ChatRunRequest`; after send, uploaded refs stay (files persist), pending list cleared.
- Remove before send: new upload → `deleteSessionFile`; project ref → drop from list only.
- Upload done → insert `@name` at caret (or `@"name"` if spaced) and add ref with `source: "upload"`.

- [ ] **Step 1: Write component tests** (paperclip opens picker; drag adds files; paste image adds upload; progress percent shown; failure shows retry; remove new upload calls DELETE; remove project ref only drops).
- [ ] **Step 2: Implement** `customRequest` wrapper around `uploadSessionFiles` with per-file progress; `Upload` with `multiple`, `accept` unrestricted, `pastable`, drop zone over composer box; toolbar `<IconAction label="上传文件" icon={<PaperClipOutlined />}>`; strip above input with `Progress`, image `Image.PreviewGroup` thumbnails, failure retry.
- [ ] **Step 3: Implement delete/insert logic in ChatPage**; send gathers references.
- [ ] **Step 4: Run tests + typecheck + build**, commit `feat: add composer upload strip with drag and paste`.

---

### Task 10: Reference chips on user messages + invalid-file display

**Files:**
- Modify: `frontend/src/pages/chat/ChatPage.tsx` (user bubble renders chips)
- Modify: `frontend/src/pages/chat/messageParts.tsx` (or inline chip component)
- Modify: `frontend/src/styles/chat.css`
- Test: `frontend/src/pages/ChatPage.test.tsx`

- [ ] **Step 1: Write test** — user message with `references` renders chips (source label + name); image ref shows thumbnail via content URL; 404 content marks chip "文件不可用"; click opens content URL in new tab.
- [ ] **Step 2: Implement** chip row under bubble; availability check via `fetch(contentUrl, {method:"HEAD"})` per chip (bounded, cached per path); edit mode restores refs into composer.
- [ ] **Step 3: Run tests**, commit `feat: render message file references`.

---

### Task 11: Backend integration tests (references lifecycle)

**Files:**
- Create/extend: `tests/test_session_files.py`, `tests/test_web_chat.py`, `tests/test_cloud_snapshots.py`

Cover: upload → reference → chat run (bridge node contains references) → transcript contains references → rewind copies uploads + references → branch copies uploads → snapshot materialize/restore round-trip keeps uploads under workspace; old snapshot with legacy uploads auto-migrates on restore; binary and image files never injected into model text (assert model prompt builder receives only text content); forged/missing/cross-session refs rejected.

- [ ] **Step 1: Write tests**, run, fix, commit `test: cover session file reference lifecycle`.

---

### Task 12: Full validation pass

- [ ] Run `python -m pytest -q` (external `--basetemp`, `-p no:cacheprovider`) — all green.
- [ ] Run `python -m ruff check .` and `python -m ruff format --check .` — clean.
- [ ] Run frontend `vitest run`, `tsc --noEmit`, `vite build` — clean.
- [ ] Run `antd lint frontend/src/pages/chat` — no new violations.
- [ ] Start `python -m backend.api`, open GUI, take desktop + mobile screenshots of the `@`-menu and upload strip above the input; confirm no overlap with input box.
- [ ] Commit final fixes.

---

## Self-Review Checklist

- Spec coverage: upload API (T2/T3), search (T2), content+security headers (T3), delete (T3), streaming atomic batch + limits (T2), name cleanup/conflict (T2), clipboard naming (T9/T2 sanitize), layout migration + sync/branch/rewind/guest copy + restore migration (T1), dual-root search (T2), references validation + persistence (T4), expander bypass (T4), read-only upload tool (T5), projection/refresh/run-controller/edit/rewind/branch propagation (T6/T10), invalid-file display (T10), composer `@` rules + IME + mutual exclusion + quoting (T8), antd Upload customRequest/multiple/drag/pastable + strip + auto-insert (T9), test plan coverage (T11/T12).
- Placeholder scan: no TBD/TODO; fixtures cross-referenced to real auth helpers.
- Type consistency: `source` values `"project"|"upload"` everywhere; `path` relative; response keys `source/path/name/size/mime/mtime/is_image` identical backend↔frontend.
