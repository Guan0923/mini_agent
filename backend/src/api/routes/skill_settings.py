"""User-level Skill discovery and management routes."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, StrictBool

from backend.skills import SkillCatalog, SkillConfigurationError

from ..directory_picker import DirectoryPickerBusyError, pick_directory

router = APIRouter(prefix="/api/settings/skills", tags=["settings"])


class EnabledPayload(BaseModel):
    enabled: StrictBool


def _state(request: Request):
    return request.app.state.web


def _skill_directory(root: Path, directory: str) -> Path:
    if not directory or directory in {".", ".."} or Path(directory).name != directory:
        raise HTTPException(status_code=404, detail="Skill directory not found.")
    raw_candidate = root / directory
    is_junction = getattr(raw_candidate, "is_junction", lambda: False)
    if raw_candidate.is_symlink() or is_junction():
        raise HTTPException(status_code=404, detail="Skill directory not found.")
    try:
        resolved_root = root.resolve(strict=True)
        candidate = raw_candidate.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Skill directory not found.") from exc
    if candidate.parent != resolved_root or not candidate.is_dir():
        raise HTTPException(status_code=404, detail="Skill directory not found.")
    return candidate


def _payload(request: Request) -> dict[str, object]:
    state = _state(request)
    capabilities = state.settings.capability_config()
    disabled = {str(item) for item in state.settings.skill_config()["disabled"]}
    try:
        definitions = SkillCatalog.discover(global_root=state.paths.skills_dir).definitions()
    except SkillConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "enabled": bool(capabilities["skills"]),
        "skills": [
            {
                "directory": item.manifest.parent.name,
                "name": item.name,
                "description": item.description,
                "metadata": dict(item.metadata),
                "allowed_tools": list(item.allowed_tools),
                "root": item.root,
                "enabled": item.manifest.parent.name not in disabled,
            }
            for item in definitions
        ],
    }


@router.get("")
def get_skills(request: Request) -> dict[str, object]:
    return _payload(request)


@router.put("/enabled")
def update_skills_enabled(body: EnabledPayload, request: Request) -> dict[str, object]:
    try:
        _state(request).settings.update_capability_config({"skills": body.enabled})
        return _payload(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/{directory}/enabled")
def update_skill_enabled(directory: str, body: EnabledPayload, request: Request) -> dict[str, object]:
    state = _state(request)
    _skill_directory(state.paths.skills_dir, directory)
    try:
        state.settings.update_skill_enabled(directory, body.enabled)
        return _payload(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/import", status_code=201, response_model=None)
def import_skill(request: Request) -> Response | dict[str, str]:
    try:
        source = pick_directory(request, title="选择要导入的 Skill 文件夹")
    except DirectoryPickerBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if source is None:
        return Response(status_code=204)
    source = Path(source)
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        raise HTTPException(status_code=422, detail="所选文件夹根目录必须包含 SKILL.md。")
    state = _state(request)
    target = state.paths.skills_dir / source.name
    if target.exists():
        raise HTTPException(status_code=409, detail=f"Skill 文件夹 {source.name} 已存在。")
    try:
        shutil.copytree(source, target)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"导入 Skill 失败：{type(exc).__name__}") from exc
    return {"directory": target.name}


@router.delete("/{directory}", status_code=204)
def delete_skill(directory: str, request: Request) -> Response:
    state = _state(request)
    target = _skill_directory(state.paths.skills_dir, directory)
    try:
        shutil.rmtree(target)
        state.settings.update_skill_enabled(directory, True)
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"删除 Skill 失败：{type(exc).__name__}") from exc
    return Response(status_code=204)
