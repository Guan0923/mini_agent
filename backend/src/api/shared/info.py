"""Read-only info endpoints: the tools and skills the agent actually sees."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from backend.configuration import validate_identity_id
from backend.skills import SkillCatalog
from backend.tools import build_tool_registry

from ..auth.dependencies import require_user
from ..auth.types import UserIdentity
from ..state import WebAppState

router = APIRouter(prefix="/api")


def _catalog_workspace(state: WebAppState, user_id: str):
    # Tool/skill discovery must not create a pseudo-session under runtime/;
    # this cache is deliberately outside the user snapshot tree.
    validate_identity_id(user_id, require_uuid=True)
    workspace = state.data_root.parent / ".mini_agent-cache" / "catalog" / user_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


@router.get("/tools")
def list_tools(request: Request, identity: UserIdentity = Depends(require_user)) -> list[dict]:
    state: WebAppState = request.app.state.web
    registry = build_tool_registry(_catalog_workspace(state, identity.id))
    return [{"name": spec.name, "description": spec.description} for spec in registry.specs()]


@router.get("/skills")
def list_skills(request: Request, identity: UserIdentity = Depends(require_user)) -> list[dict]:
    state: WebAppState = request.app.state.web
    catalog = SkillCatalog.discover(
        _catalog_workspace(state, identity.id),
        global_root=state.user_paths(identity.id).skills_dir,
    )
    return [{"name": skill.name, "description": skill.description} for skill in catalog.definitions()]


@router.get("/paths")
def user_paths(request: Request, identity: UserIdentity = Depends(require_user)) -> dict[str, str]:
    """Expose the canonical user-data contract for diagnostics and clients."""

    paths = request.app.state.web.user_paths(identity.id)
    return {
        "root": str(paths.root),
        "config_file": str(paths.config_file),
        "user_db": str(paths.user_db),
        "skills": str(paths.skills_dir),
        "rag": str(paths.rag_dir),
        "plugins": str(paths.plugins_dir),
        "mcp": str(paths.mcp_dir),
        "mcp_servers": str(paths.mcp_file),
        "mcp_trust": str(paths.mcp_trust_file),
        "mcp_resources": str(paths.mcp_resources_dir),
        "runtime": str(paths.runtime_dir),
        "sessions": str(paths.runtime_dir),
        "sync": str(paths.sync_dir),
        "sync_staging": str(paths.sync_staging_dir),
        "sync_recovery": str(paths.sync_recovery_dir),
    }
