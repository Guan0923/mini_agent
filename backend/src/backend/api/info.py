"""Read-only info endpoints: the tools and skills the agent actually sees."""

from __future__ import annotations

from fastapi import APIRouter, Request

from backend.skills import SkillCatalog
from backend.tools import build_tool_registry

from .state import WebAppState

router = APIRouter(prefix="/api")


@router.get("/tools")
def list_tools(request: Request) -> list[dict]:
    state: WebAppState = request.app.state.web
    registry = build_tool_registry(state.chat_workspace)
    return [{"name": spec.name, "description": spec.description} for spec in registry.specs()]


@router.get("/skills")
def list_skills(request: Request) -> list[dict]:
    state: WebAppState = request.app.state.web
    catalog = SkillCatalog.discover(state.chat_workspace, global_root=state.sandbox.paths.skills_dir)
    return [{"name": skill.name, "description": skill.description} for skill in catalog.definitions()]
