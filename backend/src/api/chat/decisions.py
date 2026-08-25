"""POST endpoint for run-time interactive decisions (approvals, supplements, answers)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.dependencies import require_user
from ..auth.types import UserIdentity
from .interrupts import registry

router = APIRouter(prefix="/api")


class DecisionBody(BaseModel):
    decision_id: str
    choice: str = "continue"
    supplement: str | None = None
    answers: dict[str, list[str]] | None = None


@router.post("/decisions")
def submit_decision(
    body: DecisionBody,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict:
    allowed = {
        "continue",
        "cancel",
        "deny",
        "allow_once",
        "allow_session",
        "supplement",
        "implement",
        "implement_clear_session",
        "answer",
        "back",
    }
    if body.choice not in allowed:
        raise HTTPException(status_code=422, detail=f"不支持的决策：{body.choice}")
    if body.choice == "supplement" and not (body.supplement or "").strip():
        raise HTTPException(status_code=422, detail="补充说明不能为空")
    if body.choice == "answer" and body.answers is None:
        raise HTTPException(status_code=422, detail="问题决策需要 answers")
    resolved = registry.resolve(
        body.decision_id,
        body.model_dump(exclude={"decision_id"}),
        owner_id=identity.id,
    )
    if not resolved:
        raise HTTPException(status_code=404, detail="未知或已过期的决策")
    return {"ok": True}
