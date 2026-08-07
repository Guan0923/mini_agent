"""Read-only info endpoints: the tools and skills the agent actually sees."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from backend.skills import SkillCatalog
from backend.tools import build_tool_registry

from ..auth.dependencies import require_user
from ..auth.types import UserIdentity
from ..state import WebAppState

router = APIRouter(prefix="/api")


@router.get("/tools")
def list_tools(request: Request, identity: UserIdentity = Depends(require_user)) -> list[dict]:
    state: WebAppState = request.app.state.web
    registry = build_tool_registry(state.user_workspace(identity.id))
    return [{"name": spec.name, "description": spec.description} for spec in registry.specs()]


@router.get("/skills")
def list_skills(request: Request, identity: UserIdentity = Depends(require_user)) -> list[dict]:
    state: WebAppState = request.app.state.web
    catalog = SkillCatalog.discover(
        state.user_workspace(identity.id),
        global_root=state.user_paths(identity.id).skills_dir,
    )
    return [{"name": skill.name, "description": skill.description} for skill in catalog.definitions()]
