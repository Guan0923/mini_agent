from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.storage.auth import LocalAuthStore


def _state(tmp_path: Path, project: Path) -> WebAppState:
    return WebAppState(
        tmp_path / "web",
        auth_repository=LocalAuthStore(tmp_path / "client.db"),
        project_picker=lambda: project,
    )


def test_project_agents_init_endpoint_creates_once_and_never_overwrites(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (project / "tests").mkdir()
    state = _state(tmp_path, project)

    with TestClient(create_app(state)) as client:
        assert client.post("/api/auth/guest").status_code == 200
        created_project = client.post("/api/projects")
        assert created_project.status_code == 200, created_project.text
        project_id = created_project.json()["project"]["project_id"]

        initialized = client.post(f"/api/projects/{project_id}/agents/init")
        assert initialized.status_code == 200, initialized.text
        payload = initialized.json()
        assert payload["created"] is True
        assert payload["path"] == "AGENTS.md"
        assert payload["byte_count"] == len(payload["content"].encode("utf-8"))
        assert (project / "AGENTS.md").read_text(encoding="utf-8") == payload["content"]

        original = (project / "AGENTS.md").read_bytes()
        conflict = client.post(f"/api/projects/{project_id}/agents/init")
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "项目根目录已存在 AGENTS.md，未进行覆盖。"
        assert (project / "AGENTS.md").read_bytes() == original


def test_project_agents_init_requires_an_active_owned_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state = _state(tmp_path, project)

    with TestClient(create_app(state)) as client:
        assert client.post("/api/projects/missing/agents/init").status_code == 401
        assert client.post("/api/auth/guest").status_code == 200
        assert client.post("/api/projects/missing/agents/init").status_code == 404

        created_project = client.post("/api/projects").json()
        project_id = created_project["project"]["project_id"]
        assert client.post(f"/api/projects/{project_id}/remove").status_code == 200
        removed = client.post(f"/api/projects/{project_id}/agents/init")
        assert removed.status_code == 404
        assert not (project / "AGENTS.md").exists()
