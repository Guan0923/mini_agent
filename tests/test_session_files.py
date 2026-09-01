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


@pytest.fixture()
def state(tmp_path: Path) -> WebAppState:
    return WebAppState(tmp_path / "web")


@pytest.fixture()
def client(state: WebAppState) -> TestClient:
    test_client = TestClient(create_app(state))
    session = test_client.post("/api/sidebar-threads", json={}).json()
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
    assert content.headers["cache-control"] == "no-store"
    assert content.headers["content-disposition"].startswith("attachment")

    available = client.head(
        f"/api/sessions/{client.session_id}/files/content?source=upload&path=bin.dat"  # type: ignore[attr-defined]
    )
    assert available.status_code == 200
    assert available.content == b""
    assert available.headers["x-content-type-options"] == "nosniff"

    missing = client.head(
        f"/api/sessions/{client.session_id}/files/content?source=upload&path=missing.dat"  # type: ignore[attr-defined]
    )
    assert missing.status_code == 404


def test_project_file_head_probe(client: TestClient, state: WebAppState) -> None:
    workspace = state.session_workspace(client.session_id)  # type: ignore[attr-defined]
    project_file = workspace / "biome.jsonc"
    project_file.write_text("{}", encoding="utf-8")

    available = client.head(
        f"/api/sessions/{client.session_id}/files/content?source=project&path=biome.jsonc"  # type: ignore[attr-defined]
    )
    assert available.status_code == 200
    assert available.content == b""


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


def test_upload_isolation_between_sessions(state: WebAppState) -> None:
    with TestClient(create_app(state)) as first:
        session_a = first.post("/api/sidebar-threads", json={}).json()["session_id"]
        uploaded = first.post(
            f"/api/sessions/{session_a}/files",
            files=[("files", ("secret.txt", io.BytesIO(b"secret"), "text/plain"))],
        )
        assert uploaded.status_code == 200
        session_b = first.post("/api/sidebar-threads", json={}).json()["session_id"]
        missing = first.get(f"/api/sessions/{session_b}/files/content?source=upload&path=secret.txt")
        assert missing.status_code == 404


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
    session_id = client.session_id  # type: ignore[attr-defined]
    workspace = state.session_workspace(session_id)
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
