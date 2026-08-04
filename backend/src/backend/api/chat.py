"""SSE chat endpoint that drives the real agent against a persistent workspace.

Supports two modes:
- default: auto-approve every tool call (used by the web frontend);
- ``interactive=True``: pauses on approvals and asks the client to decide via
  ``POST /api/decisions`` (used by the TUI client).
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.providers import ModelConfigurationError
from backend.runtime import RunnerSettings, build_application
from backend.runtime.core.events import RuntimeEvent

from .decisions import router as decisions_router
from .interrupts import auto_approve, make_interactive_interrupt
from .state import WebAppState

router = APIRouter(prefix="/api")
router.include_router(decisions_router)


class ChatRequest(BaseModel):
    prompt: str
    interactive: bool = False


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}… ({len(value) - limit} chars omitted)"


def _event_payload(event: RuntimeEvent) -> dict:
    """A small, JSON-safe slice of an event's data, mirroring the TUI presenter."""
    data = event.data
    if event.kind == "tool_call":
        return {"arguments": _truncate(json.dumps(data.get("arguments", {}), ensure_ascii=False), 600)}
    if event.kind == "tool_result":
        return {"tool": data.get("tool"), "result": _truncate(event.message, 800)}
    if event.kind == "tool_failed":
        return {"tool": data.get("tool")}
    if event.kind == "run_finished":
        return {
            "status": event.message,
            "final_answer": _truncate(data.get("final_answer", ""), 6000),
            "duration_ms": data.get("duration_ms"),
            "model_calls": data.get("model_calls"),
            "tool_calls": data.get("tool_calls"),
            "active_skills": data.get("active_skills", []),
        }
    if event.kind in {"response_delta", "response_start", "thinking_delta"}:
        return {"content": _truncate(event.message, 4000)}
    return {}


def _stream(state: WebAppState, prompt: str, interactive: bool):
    q: queue.Queue = queue.Queue()
    done = threading.Event()
    finished: dict = {}

    def sink(item) -> None:
        if isinstance(item, dict):
            q.put(item)
            return
        payload = _event_payload(item)
        if item.kind == "run_finished":
            finished.update(payload)
        q.put({"type": "event", "kind": item.kind, "message": item.message, "data": payload})

    interrupt = make_interactive_interrupt(sink) if interactive else auto_approve

    def worker() -> None:
        app = None
        try:
            app = build_application(
                state.chat_workspace,
                planner_name="llm",
                settings=RunnerSettings(log_full_messages=True),
                project_mcp_enabled=False,
            )
            conversation = app.open_conversation()
            run_state = conversation.run_task(prompt, mode="agent", on_event=sink, interrupt=interrupt)
            q.put(
                {
                    "type": "done",
                    "status": run_state.status,
                    "final_answer": run_state.final_answer or "",
                    "metrics": {
                        "duration_ms": finished.get("duration_ms"),
                        "model_calls": finished.get("model_calls"),
                        "tool_calls": finished.get("tool_calls"),
                        "active_skills": finished.get("active_skills", []),
                    },
                }
            )
        except ModelConfigurationError as exc:
            q.put({"type": "error", "message": f"模型未配置：{exc}"})
        except Exception as exc:
            q.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            if app is not None:
                try:
                    app.close()
                except Exception:
                    pass
            done.set()

    threading.Thread(target=worker, daemon=True).start()

    async def generator():
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                if done.is_set():
                    break
                await asyncio.sleep(0.05)
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return generator()


@router.post("/chat")
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    state: WebAppState = request.app.state.web
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    return StreamingResponse(
        _stream(state, body.prompt.strip(), body.interactive),
        media_type="text/event-stream",
    )
