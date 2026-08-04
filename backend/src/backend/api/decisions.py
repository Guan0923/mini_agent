"""POST endpoint for run-time interactive decisions (approvals, supplements, answers)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth_dependencies import require_user
from .auth_types import UserIdentity
from .interrupts import registry

# Included under the chat router's /api prefix, so it carries no prefix itself.
router = APIRouter()


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
    resolved = registry.resolve(
        body.decision_id,
        body.model_dump(exclude={"decision_id"}),
        owner_id=identity.id,
    )
    if not resolved:
        raise HTTPException(status_code=404, detail="未知或已过期的决策")
    return {"ok": True}
