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
    assert items[0]["path"] == "workspace:uploads/bin.dat"
    assert items[0]["display_path"] == items[0]["path"]
    assert items[0]["name"] == "bin.dat"
    assert items[0]["size"] == len(payload)
    assert items[0]["is_image"] is False

    content = client.get(
        f"/api/sessions/{client.session_id}/files/content",  # type: ignore[attr-defined]
        params={"source": "upload", "path": items[0]["path"]},
    )
    assert content.status_code == 200
    assert content.content == payload
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["cache-control"] == "no-store"
    assert content.headers["content-disposition"].startswith("attachment")

    available = client.head(
        f"/api/sessions/{client.session_id}/files/content",  # type: ignore[attr-defined]
        params={"source": "upload", "path": items[0]["path"]},
    )
    assert available.status_code == 200
    assert available.content == b""
    assert available.headers["x-content-type-options"] == "nosniff"

    missing = client.head(
        f"/api/sessions/{client.session_id}/files/content",  # type: ignore[attr-defined]
        params={"source": "upload", "path": str(Path(items[0]["path"]).with_name("missing.dat"))},
    )
    assert missing.status_code == 404


def test_workspace_file_head_probe(client: TestClient, state: WebAppState) -> None:
    workspace = state.session_workspace(client.session_id)  # type: ignore[attr-defined]
    project_file = workspace / "biome.jsonc"
    project_file.write_text("{}", encoding="utf-8")

    available = client.head(
        f"/api/sessions/{client.session_id}/files/content",  # type: ignore[attr-defined]
        params={"source": "workspace", "path": str(project_file.resolve())},
    )
    assert available.status_code == 200
    assert available.content == b""


def test_image_preview_is_inline_and_download_is_attachment(client: TestClient) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    uploaded = _upload(client, [("shot.png", payload)]).json()[0]
    preview = client.get(
        f"/api/sessions/{client.session_id}/files/content",  # type: ignore[attr-defined]
        params={"source": "upload", "path": uploaded["path"]},
    )
    assert preview.status_code == 200
    assert preview.headers["content-disposition"].startswith("inline")
    assert preview.headers["content-type"].startswith("image/")
    download = client.get(
        f"/api/sessions/{client.session_id}/files/content",  # type: ignore[attr-defined]
        params={"source": "upload", "path": uploaded["path"], "download": "true"},
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
        uploaded_path = uploaded.json()[0]["path"]
        session_b = first.post("/api/sidebar-threads", json={}).json()["session_id"]
        missing = first.get(
            f"/api/sessions/{session_b}/files/content",
            params={"source": "upload", "path": uploaded_path},
        )
        assert missing.status_code == 404
        escaped = first.get(
            f"/api/sessions/{session_b}/files/content",
            params={"source": "upload", "path": str(state.paths.session_uploads(session_a) / "secret.txt")},
        )
        assert escaped.status_code == 400


def test_upload_name_sanitization_and_conflict_naming(client: TestClient) -> None:
    response = _upload(client, [("../evil.txt", b"one"), ("..\\evil.txt", b"two"), ("evil.txt", b"three")])
    assert response.status_code == 200, response.text
    paths = {item["display_path"] for item in response.json()}
    # Separators become underscores; leading dots are stripped so a traversal
    # name cannot survive; conflicts are numbered from (2).
    assert paths == {"workspace:uploads/_evil.txt", "workspace:uploads/_evil (2).txt", "workspace:uploads/evil.txt"}
    assert all(item["path"] == item["display_path"] for item in response.json())

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
    assert [item["source"] for item in search.json()] == ["workspace"]
    assert search.json()[0]["path"] == "workspace:project-note.md"
    assert search.json()[0]["display_path"] == "workspace:project-note.md"

    search = client.get(f"/api/sessions/{session_id}/files?q=uploaded")
    assert [item["source"] for item in search.json()] == ["upload"]
    assert search.json()[0]["is_image"] is True

    # Uploads below the workspace are not duplicated under the project source.
    search = client.get(f"/api/sessions/{session_id}/files?q=uploaded.png&limit=50")
    assert len(search.json()) == 1


def test_delete_upload_only(client: TestClient) -> None:
    uploaded = _upload(client, [("keep.txt", b"keep")]).json()[0]
    delete = client.delete(
        f"/api/sessions/{client.session_id}/files",  # type: ignore[attr-defined]
        params={"source": "upload", "path": uploaded["path"]},
    )
    assert delete.status_code == 200
    gone = client.get(
        f"/api/sessions/{client.session_id}/files/content",  # type: ignore[attr-defined]
        params={"source": "upload", "path": uploaded["path"]},
    )
    assert gone.status_code == 404

    project_delete = client.delete(
        f"/api/sessions/{client.session_id}/files",  # type: ignore[attr-defined]
        params={"source": "project", "path": uploaded["path"]},
    )
    assert project_delete.status_code == 403


def test_store_resolve_and_normalize_references(tmp_path: Path) -> None:
    root = tmp_path / "root"
    paths = ClientPaths(root)
    uploads = paths.session_uploads("session_x")
    uploads.mkdir(parents=True)
    store = SessionFileStore(paths, "session_x")
    upload = uploads / "note.txt"
    upload.write_text("upload", encoding="utf-8")
    assert store.resolve("upload", str(upload)) == upload.resolve()
    assert store.normalize_references([{"source": "upload", "path": str(upload), "display_path": "forged.txt"}]) == [
        {"source": "upload", "path": "workspace:uploads/note.txt", "display_path": "workspace:uploads/note.txt"}
    ]

    with pytest.raises(SessionFileError):
        store.resolve("upload", str(upload.with_name("missing.txt")))
    with pytest.raises(SessionFileError):
        store.resolve("upload", "../outside.txt")
    outside = root / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(SessionFileError):
        store.resolve("upload", str(outside.resolve()))
    with pytest.raises(SessionFileError):
        store.resolve("project", str(upload.resolve()))
    with pytest.raises(SessionFileError):
        store.resolve("upload", str(upload.parent.resolve()))
    with pytest.raises(SessionFileError):
        store.resolve("unknown", str(upload.resolve()))


def test_store_resolve_rejects_symbolic_link_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    paths = ClientPaths(root)
    uploads = paths.session_uploads("session_x")
    uploads.mkdir(parents=True)
    outside = root / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = uploads / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接。")
    store = SessionFileStore(paths, "session_x")
    with pytest.raises(SessionFileError):
        store.resolve("upload", str(link))


def test_store_resolve_rejects_symbolic_link_upload_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    paths = ClientPaths(root)
    upload_root = paths.session_uploads("session_x")
    upload_root.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("secret", encoding="utf-8")
    try:
        upload_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接。")
    store = SessionFileStore(paths, "session_x")
    with pytest.raises(SessionFileError):
        store.resolve("upload", str(upload_root / "note.txt"))


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
