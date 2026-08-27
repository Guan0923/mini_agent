"""Read-only info endpoints: the tools and skills the agent actually sees."""

from __future__ import annotations

from fastapi import APIRouter, Request

from backend.skills import SkillCatalog
from backend.tools import build_tool_registry

from ..state import WebAppState

router = APIRouter(prefix="/api")


def _catalog_workspace(state: WebAppState):
    # Listing schemas does not execute tools, so the existing local data root
    # is a safe confinement anchor without creating a pseudo-session or cache.
    return state.paths.root


@router.get("/tools")
def list_tools(request: Request) -> list[dict]:
    state: WebAppState = request.app.state.web
    registry = build_tool_registry(_catalog_workspace(state))
    return [{"name": spec.name, "description": spec.description} for spec in registry.specs()]


@router.get("/skills")
def list_skills(request: Request) -> list[dict]:
    state: WebAppState = request.app.state.web
    catalog = SkillCatalog.discover(global_root=state.paths.skills_dir)
    return [{"name": skill.name, "description": skill.description} for skill in catalog.definitions()]


@router.get("/paths")
def local_paths(request: Request) -> dict[str, str]:
    """Expose the canonical local-data contract for diagnostics and clients."""

    paths = request.app.state.web.paths
    return {
        "root": str(paths.root),
        "config_file": str(paths.config_file),
        "state_db": str(paths.state_db),
        "projects_db": str(paths.projects_db),
        "skills": str(paths.skills_dir),
        "plugins": str(paths.plugins_dir),
        "mcp": str(paths.mcp_dir),
        "mcp_servers": str(paths.mcp_file),
        "mcp_trust": str(paths.mcp_trust_file),
        "mcp_resources": str(paths.mcp_resources_dir),
        "runtime": str(paths.runtime_dir),
        "sessions": str(paths.runtime_dir),
    }
