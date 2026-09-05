"""Real-file checks for session/project path names across API and tools."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.session_files.store import SessionFileError, SessionFileStore
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.domain.file_paths import ScopedPaths
from backend.tools import ToolError, WorkspaceFiles
from backend.tools.filesystem import normalized_workspace_path


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    workspace, project = tmp_path / "workspace", tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    (workspace / "same.txt").write_text("session", encoding="utf-8")
    (project / "same.txt").write_text("project", encoding="utf-8")
    return workspace, project


def test_tools_use_explicit_roots_and_never_fall_back(roots: tuple[Path, Path]) -> None:
    workspace, project = roots
    files = WorkspaceFiles(workspace, project_workspace=project)
    assert files.read_file("same.txt") == "project:same.txt: lines 1-1 of 1\n1 | project"
    assert files.read_file("workspace:same.txt").endswith("1 | session")
    assert files.read_file(str(project / "same.txt")) == files.read_file("project:same.txt")
    (workspace / "only-session.txt").write_text("session only", encoding="utf-8")
    with pytest.raises(ToolError, match="Not a file: project:only-session.txt"):
        files.read_file("only-session.txt")
    files.write_file("created/note.txt", "one")
    files.edit_file("project:created/note.txt", 1, 1, ["one"], ["two"])
    files.write_file("workspace:created/note.txt", "session new")
    assert (project / "created/note.txt").read_text(encoding="utf-8") == "two"
    assert (workspace / "created/note.txt").read_text(encoding="utf-8") == "session new"
    assert files.glob("*.txt", "project:") == "project:same.txt"
    assert files.grep("session", "workspace:same.txt") == "workspace:same.txt:1:session"


def test_ordinary_tools_default_to_workspace(roots: tuple[Path, Path]) -> None:
    workspace, _ = roots
    files = WorkspaceFiles(workspace)
    assert files.read_file("same.txt").startswith("workspace:same.txt:")
    files.create_directory("new/folder")
    assert (workspace / "new/folder").is_dir()
    with pytest.raises(ToolError, match="当前会话没有项目目录"):
        files.read_file("project:same.txt")


@pytest.mark.parametrize(
    "path",
    [
        "workspace:../same.txt",
        "project:../same.txt",
        "workspace:/same.txt",
        "workspace:C:/same.txt",
        "workspace:same.txt:stream",
        "unknown:same.txt",
        "C:relative.txt",
        "../same.txt",
    ],
)
def test_malformed_paths_do_not_escape(roots: tuple[Path, Path], path: str) -> None:
    with pytest.raises(ToolError):
        WorkspaceFiles(roots[0], project_workspace=roots[1]).read_file(path)


def test_aliases_share_write_lock_and_scopes_remain_distinct(roots: tuple[Path, Path]) -> None:
    workspace, project = roots
    project_key = normalized_workspace_path(roots, str(project / "same.txt"))
    assert project_key == normalized_workspace_path(roots, "project:./same.txt")
    assert project_key == normalized_workspace_path(roots, "same.txt")
    assert project_key != normalized_workspace_path(roots, "workspace:same.txt")
    assert normalized_workspace_path((workspace, workspace), "project:same.txt") == normalized_workspace_path(
        workspace, "same.txt"
    )
    assert normalized_workspace_path(workspace, "same.txt") == normalized_workspace_path(
        workspace, str(workspace / "same.txt")
    )


def test_shared_paths_reject_linked_ancestors(roots: tuple[Path, Path], tmp_path: Path) -> None:
    workspace, project = roots
    link = workspace / "linked"
    try:
        link.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links is not permitted on this system.")
    with pytest.raises(ValueError, match="Symbolic links"):
        ScopedPaths(workspace, project).resolve("workspace:linked/same.txt")
    assert "linked" not in WorkspaceFiles(workspace, project_workspace=project).glob("**/*")


def test_search_and_references_cover_full_workspace_without_duplicate_uploads(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    paths.ensure_session("session")
    workspace = paths.session_workspace("session")
    project = tmp_path / "project"
    project.mkdir()
    (workspace / "generated").mkdir()
    (workspace / "generated/report.txt").write_text("generated", encoding="utf-8")
    (project / "report.txt").write_text("project", encoding="utf-8")
    (workspace / "uploads/report.txt").write_text("upload", encoding="utf-8")
    store = SessionFileStore(paths, "session", project)
    results = store.search("report")
    assert {(item["source"], item["path"]) for item in results} == {
        ("workspace", "workspace:generated/report.txt"),
        ("project", "project:report.txt"),
        ("upload", "workspace:uploads/report.txt"),
    }
    assert len(store.search("workspace:report")) == 2
    assert [item["path"] for item in store.search("project:report")] == ["project:report.txt"]
    assert len(store.search("workspace:uploads/")) == 1
    refs = store.normalize_references(
        [
            {"source": "project", "path": "report.txt", "display_path": "forged"},
            {"source": "project", "path": str(project / "report.txt"), "display_path": "forged"},
        ]
    )
    assert refs == [{"source": "project", "path": "project:report.txt", "display_path": "project:report.txt"}]
    for source, path in (("project", "workspace:generated/report.txt"), ("upload", "workspace:generated/report.txt")):
        with pytest.raises(SessionFileError):
            store.resolve(source, path)
    with pytest.raises(SessionFileError):
        store.delete_upload("workspace:generated/report.txt")
    assert (workspace / "generated/report.txt").is_file()


def test_project_api_upload_search_preview_and_delete(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "data")
    project = tmp_path / "project"
    project.mkdir()
    (project / "same.txt").write_text("project", encoding="utf-8")
    created = state.projects.create(project)
    with TestClient(create_app(state)) as client:
        sidebar = client.post(f"/api/projects/{created.project_id}/sessions", json={"title": "paths"}).json()["session"]
        session = sidebar["session_id"]
        workspace = state.paths.session_workspace(session)
        (workspace / "same.txt").write_text("session", encoding="utf-8")
        base = f"/api/sessions/{session}/files"
        uploaded = client.post(base, files=[("files", ("报告.txt", b"uploaded", "text/plain"))]).json()[0]
        assert uploaded["path"] == uploaded["display_path"] == "workspace:uploads/报告.txt"
        assert (workspace / "uploads/报告.txt").read_bytes() == b"uploaded"
        assert {item["path"] for item in client.get(base, params={"q": "same"}).json()} == {
            "workspace:same.txt",
            "project:same.txt",
        }
        for source, path, content in (
            ("project", "project:same.txt", "project"),
            ("workspace", "workspace:same.txt", "session"),
        ):
            response = client.get(base + "/content", params={"source": source, "path": path})
            assert response.status_code == 200 and response.text == content
        assert client.delete(base, params={"source": "upload", "path": "workspace:same.txt"}).status_code == 400
        assert client.delete(base, params={"source": "upload", "path": uploaded["path"]}).status_code == 200
