"""Session file upload/search/content/delete API and store tests."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.session_files.store import (
    MAX_BATCH_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES_PER_BATCH,
    SessionFileError,
    SessionFileStore,
)
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.storage.auth import LocalAuthStore


@pytest.fixture()
def state(tmp_path: Path) -> WebAppState:
    return WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))


@pytest.fixture()
def client(state: WebAppState) -> TestClient:
    test_client = TestClient(create_app(state))
    test_client.post("/api/auth/guest")
    session = test_client.post("/api/sessions", json={}).json()
    test_client.session_id = session["session_id"]  # type: ignore[attr-defined]
    return test_client


def _upload(client: TestClient, files: list[tuple[str, bytes]], session_id: str | None = None) -> object:
    target = session_id or client.session_id  # type: ignore[attr-defined]
    return client.post(
        f"/api/sessions/{target}/files",
        files=[("files", (name, io.BytesIO(content), "application/octet-stream")) for name, content in files],
    )


def test_upload_round_trip_and_binary_integrity(client: TestClient) -> None:
    payload = bytes(range(256)) * 64
    response = _upload(client, [("bin.dat", payload)])
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 1
    assert items[0]["source"] == "upload"
    assert items[0]["path"] == "bin.dat"
    assert items[0]["name"] == "bin.dat"
    assert items[0]["size"] == len(payload)
    assert items[0]["is_image"] is False

    content = client.get(
        f"/api/sessions/{client.session_id}/files/content?source=upload&path=bin.dat"  # type: ignore[attr-defined]
    )
    assert content.status_code == 200
    assert content.content == payload
    assert content.headers["x-content-type-options"] == "nosniff"
    assert "private" in content.headers["cache-control"]
    assert content.headers["content-disposition"].startswith("attachment")


def test_image_preview_is_inline_and_download_is_attachment(client: TestClient) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    _upload(client, [("shot.png", payload)])
    preview = client.get(
        f"/api/sessions/{client.session_id}/files/content?source=upload&path=shot.png"  # type: ignore[attr-defined]
    )
    assert preview.status_code == 200
    assert preview.headers["content-disposition"].startswith("inline")
    assert preview.headers["content-type"].startswith("image/")
    download = client.get(
        f"/api/sessions/{client.session_id}/files/content?source=upload&path=shot.png&download=true"  # type: ignore[attr-defined]
    )
    assert download.headers["content-disposition"].startswith("attachment")


def test_upload_isolation_between_sessions_and_users(state: WebAppState) -> None:
    from backend.api.auth.service import COOKIE_NAME
    from backend.storage.auth.types import UserIdentity

    with TestClient(create_app(state)) as first:
        first.post("/api/auth/guest")
        session_a = first.post("/api/sessions", json={}).json()["session_id"]
        uploaded = first.post(
            f"/api/sessions/{session_a}/files",
            files=[("files", ("secret.txt", io.BytesIO(b"secret"), "text/plain"))],
        )
        assert uploaded.status_code == 200
        session_b = first.post("/api/sessions", json={}).json()["session_id"]
        missing = first.get(f"/api/sessions/{session_b}/files/content?source=upload&path=secret.txt")
        assert missing.status_code == 404

        # A different authenticated identity must not read the first user's
        # session files even when it knows the session id.
        second = TestClient(create_app(state))
        other = state.auth.upsert_identity(
            UserIdentity("223e4567-e89b-12d3-a456-426614174000", "other@example.com", "account")
        )
        token = state.auth.create_session(other.id, "browser")
        second.cookies.set(COOKIE_NAME, token)
        other_user = second.get(f"/api/sessions/{session_a}/files/content?source=upload&path=secret.txt")
        assert other_user.status_code == 404
        other_session = second.post("/api/sessions", json={}).json()["session_id"]
        other_upload = second.post(
            f"/api/sessions/{other_session}/files",
            files=[("files", ("mine.txt", io.BytesIO(b"mine"), "text/plain"))],
        )
        assert other_upload.status_code == 200


def test_upload_name_sanitization_and_conflict_naming(client: TestClient) -> None:
    response = _upload(client, [("../evil.txt", b"one"), ("..\\evil.txt", b"two"), ("evil.txt", b"three")])
    assert response.status_code == 200, response.text
    paths = {item["path"] for item in response.json()}
    # Separators become underscores; leading dots are stripped so a traversal
    # name cannot survive; conflicts are numbered from (2).
    assert paths == {"_evil.txt", "_evil (2).txt", "evil.txt"}

    search = client.get(f"/api/sessions/{client.session_id}/files?q=evil")  # type: ignore[attr-defined]
    assert search.status_code == 200
    assert len(search.json()) == 3


def test_upload_batch_limits_and_atomicity(client: TestClient) -> None:
    too_many = _upload(client, [(f"f{i}.txt", b"x") for i in range(MAX_FILES_PER_BATCH + 1)])
    assert too_many.status_code == 400
    assert "20" in too_many.json()["detail"]

    oversized = _upload(client, [("big.bin", b"x" * (MAX_FILE_BYTES + 1))])
    assert oversized.status_code == 400

    total_oversized = _upload(
        client,
        [("a.bin", b"y" * (MAX_BATCH_BYTES // 2)), ("b.bin", b"z" * (MAX_BATCH_BYTES // 2 + 1))],
    )
    assert total_oversized.status_code == 400

    # A rejected batch must leave no partial files behind.
    listing = client.get(f"/api/sessions/{client.session_id}/files")  # type: ignore[attr-defined]
    assert listing.json() == []


def test_search_combines_project_and_upload_sources(client: TestClient, state: WebAppState) -> None:
    identity = client.get("/api/auth/me").json()
    session_id = client.session_id  # type: ignore[attr-defined]
    workspace = state.user_workspace(identity["id"], session_id)
    (workspace / "project-note.md").write_text("# project", encoding="utf-8")

    _upload(client, [("uploaded.png", b"\x89PNG\r\n\x1a\n" + b"0" * 16)])
    search = client.get(f"/api/sessions/{session_id}/files?q=note")
    assert search.status_code == 200
    assert [item["source"] for item in search.json()] == ["project"]
    assert search.json()[0]["path"] == "project-note.md"

    search = client.get(f"/api/sessions/{session_id}/files?q=uploaded")
    assert [item["source"] for item in search.json()] == ["upload"]
    assert search.json()[0]["is_image"] is True

    # Uploads below the workspace are not duplicated under the project source.
    search = client.get(f"/api/sessions/{session_id}/files?q=uploaded.png&limit=50")
    assert len(search.json()) == 1


def test_delete_upload_only(client: TestClient) -> None:
    _upload(client, [("keep.txt", b"keep")])
    delete = client.delete(
        f"/api/sessions/{client.session_id}/files?source=upload&path=keep.txt"  # type: ignore[attr-defined]
    )
    assert delete.status_code == 200
    gone = client.get(
        f"/api/sessions/{client.session_id}/files/content?source=upload&path=keep.txt"  # type: ignore[attr-defined]
    )
    assert gone.status_code == 404

    project_delete = client.delete(
        f"/api/sessions/{client.session_id}/files?source=project&path=whatever.txt"  # type: ignore[attr-defined]
    )
    assert project_delete.status_code == 403


def test_store_resolve_rejects_missing_and_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "workspace" / "uploads").mkdir(parents=True)
    paths = ClientPaths(root)
    store = SessionFileStore(paths, "session_x")
    with pytest.raises(SessionFileError):
        store.resolve("upload", "missing.txt")
    with pytest.raises(SessionFileError):
        store.resolve("upload", "../outside.txt")
    with pytest.raises(SessionFileError):
        store.resolve("upload", "/absolute.txt")
    with pytest.raises(SessionFileError):
        store.resolve("unknown", "x.txt")


def test_store_sanitize_and_unique_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "workspace" / "uploads").mkdir(parents=True)
    assert SessionFileStore.sanitize_name("a/b\\c:d.txt") == "a_b_c_d.txt"
    assert SessionFileStore.sanitize_name("") == "file"
    assert SessionFileStore.sanitize_name("..") == "file"
    uploads = root / "workspace" / "uploads"
    (uploads / "x.txt").write_text("1", encoding="utf-8")
    target, name = SessionFileStore.unique_target(uploads, "x.txt")
    assert name == "x (2).txt"


# ---------------------------------------------------------------------------
# References lifecycle: upload -> chat node -> transcript -> branch/rewind.
# ---------------------------------------------------------------------------


def _upload_and_reference(client: TestClient, session_id: str, name: str, content: bytes) -> str:
    uploaded = client.post(
        f"/api/sessions/{session_id}/files",
        files=[("files", (name, io.BytesIO(content), "application/octet-stream"))],
    )
    assert uploaded.status_code == 200, uploaded.text
    return uploaded.json()[0]["path"]


def test_chat_rejects_missing_and_forged_references(client: TestClient, state: WebAppState) -> None:
    session_id = client.session_id  # type: ignore[attr-defined]
    missing = client.post(
        "/api/chat",
        json={
            "prompt": "查看文件",
            "session_id": session_id,
            "references": [{"source": "upload", "path": "does-not-exist.txt"}],
        },
    )
    assert missing.status_code == 422

    forged = client.post(
        "/api/chat",
        json={
            "prompt": "查看文件",
            "session_id": session_id,
            "references": [{"source": "upload", "path": "../state.db"}],
        },
    )
    assert forged.status_code == 422


def test_reference_survives_chat_node_transcript_and_rewind(client: TestClient, state: WebAppState) -> None:
    from backend.api.sessions.routes import _store
    from backend.runtime.node_bridge import RuntimeEventNodeBridge

    identity = client.get("/api/auth/me").json()
    store = _store(state, identity["id"])
    session_id = client.session_id  # type: ignore[attr-defined]
    path = _upload_and_reference(client, session_id, "notes.md", b"# notes")

    frames: list[object] = []
    bridge = RuntimeEventNodeBridge(
        store,
        session_id=session_id,
        prompt="请分析 @notes.md",
        user=identity["id"],
        provider="chat_completions",
        provider_name="default",
        model="demo-chat",
        references=[{"source": "upload", "path": path}],
        emit=frames.append,
    )
    bridge.start()
    nodes = store.load_nodes(session_id)
    user_nodes = [node for node in nodes if node.role == "user"]
    assert user_nodes[-1].message.get("references") == [{"source": "upload", "path": path}]

    transcript = client.get(f"/api/sessions/{session_id}/transcript").json()
    user_entries = [entry for entry in transcript if entry["role"] == "user"]
    assert user_entries[-1]["references"] == [{"source": "upload", "path": path}]

    # Rewind copies the uploads and keeps references in the new session.
    rewound = client.post(
        f"/api/sessions/{session_id}/rewind",
        json={"title": "回溯", "source_node_id": user_nodes[-1].id},
    )
    assert rewound.status_code == 200, rewound.text
    target_id = rewound.json()["session_id"]
    copied = client.get(f"/api/sessions/{target_id}/files/content?source=upload&path={path}")
    assert copied.status_code == 200
    assert copied.content == b"# notes"
    target_transcript = client.get(f"/api/sessions/{target_id}/transcript").json()
    assert any(entry.get("references") == [{"source": "upload", "path": path}] for entry in target_transcript)


def test_branch_copies_uploads_and_references(client: TestClient, state: WebAppState) -> None:
    from backend.api.sessions.routes import _store
    from backend.runtime.node_bridge import RuntimeEventNodeBridge

    identity = client.get("/api/auth/me").json()
    store = _store(state, identity["id"])
    session_id = client.session_id  # type: ignore[attr-defined]
    path = _upload_and_reference(client, session_id, "data.csv", b"a,b\n1,2\n")

    bridge = RuntimeEventNodeBridge(
        store,
        session_id=session_id,
        prompt="看数据",
        user=identity["id"],
        provider="chat_completions",
        provider_name="default",
        model="demo-chat",
        references=[{"source": "upload", "path": path}],
        emit=lambda _frame: None,
    )
    bridge.start()
    nodes = store.load_nodes(session_id)
    user_node = next(node for node in nodes if node.role == "user")

    branched = client.post(
        f"/api/sessions/{session_id}/fork",
        json={"title": "分支", "source_node_id": user_node.id},
    )
    assert branched.status_code == 200, branched.text
    target_id = branched.json()["session_id"]
    copied = client.get(f"/api/sessions/{target_id}/files/content?source=upload&path={path}")
    assert copied.status_code == 200
    assert copied.content == b"a,b\n1,2\n"
    transcript = client.get(f"/api/sessions/{target_id}/transcript").json()
    assert any(entry.get("references") == [{"source": "upload", "path": path}] for entry in transcript)


def test_binary_and_image_references_never_reach_model_text(client: TestClient, state: WebAppState) -> None:
    """Structured references stay metadata; the prompt text is untouched."""

    from backend.api.sessions.routes import _store
    from backend.runtime.conversation.references import FileReferenceExpander
    from backend.runtime.node_bridge import RuntimeEventNodeBridge

    identity = client.get("/api/auth/me").json()
    store = _store(state, identity["id"])
    session_id = client.session_id  # type: ignore[attr-defined]
    binary_path = _upload_and_reference(client, session_id, "image.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    bridge = RuntimeEventNodeBridge(
        store,
        session_id=session_id,
        prompt="看看 @image.png",
        user=identity["id"],
        provider="chat_completions",
        provider_name="default",
        model="demo-chat",
        references=[{"source": "upload", "path": binary_path}],
        emit=lambda _frame: None,
    )
    bridge.start()
    nodes = store.load_nodes(session_id)
    user_node = next(node for node in nodes if node.role == "user")

    # The canonical user node carries the binary path only as metadata.
    assert user_node.message.get("references") == [{"source": "upload", "path": binary_path}]
    content_blocks = user_node.message.get("content") or []
    joined = "".join(str(block.get("text") or "") for block in content_blocks)
    assert "PNG" not in joined
    assert binary_path in joined

    # Structured turns skip the legacy expander entirely, so the binary path
    # is never replaced with file contents.

    expander = FileReferenceExpander(_NeverReadFiles())
    expanded = expander.expand("看看 @image.png", structured=True)
    assert expanded == "看看 @image.png"


class _NeverReadFiles:
    def read_text(self, _path: str) -> str:
        raise AssertionError("structured references must not read file contents")
