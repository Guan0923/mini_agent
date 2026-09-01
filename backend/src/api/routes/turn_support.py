"""Turn lookup, user-item normalization, queue errors, and SSE startup."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from backend.domain import (
    DeliveryConflict,
    MessageQueueUnavailable,
    QueueItemConflict,
    QueueItemNotFound,
    QueueItemStateConflict,
)
from backend.domain.runtime_state import RuntimeRootState, RuntimeState

from ..chat import routes as chat_routes
from ..chat.routes import _model_config_snapshot, _stream
from ..state import WebAppState
from .turn_models import TurnExecutionConfig


def _turn(store, turn_id: str) -> RuntimeState:
    item = store.find_node(turn_id)
    if item is None:
        raise HTTPException(status_code=404, detail="未知 Turn。")
    if isinstance(item, RuntimeRootState):
        raise HTTPException(status_code=409, detail="根 Turn 仅作为消息树锚点，不能执行 Turn 操作。")
    return item


def _user_item(message: Mapping[str, object]) -> dict[str, object]:
    if message.get("role") != "user":
        raise HTTPException(status_code=422, detail="message.role 必须为 user。")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
        raise HTTPException(status_code=422, detail="user Message 必须恰好包含一个 Item。")
    item = dict(content[0])
    if item.get("type") != "text" or not isinstance(item.get("text"), str):
        raise HTTPException(status_code=422, detail="当前交互要求一个 text Item。")
    if not str(item["text"]).strip() and not item.get("references"):
        raise HTTPException(status_code=422, detail="text 或文件引用至少需要一个。")
    item["status"] = "success"
    return item


def _references(item: Mapping[str, object]) -> list[dict[str, str]]:
    raw = item.get("references", [])
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="references 必须为 list。")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in raw:
        if not isinstance(value, Mapping) or value.get("source") not in {"project", "upload"}:
            raise HTTPException(status_code=422, detail="无效的文件引用。")
        path = value.get("path")
        if not isinstance(path, str) or not path:
            raise HTTPException(status_code=422, detail="文件引用 path 不能为空。")
        key = (str(value["source"]), path)
        if key not in seen:
            seen.add(key)
            result.append({"source": key[0], "path": key[1]})
    return result


def _queue_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MessageQueueUnavailable):
        return HTTPException(status_code=503, detail="message_queue_unavailable")
    if isinstance(exc, QueueItemNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (DeliveryConflict, QueueItemConflict, QueueItemStateConflict)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="message_queue_error")


def _stream_turn(
    state: WebAppState,
    *,
    session_id: str,
    thread_id: str,
    turn_id: str,
    prompt: str,
    source_id: str | None,
    config: TurnExecutionConfig,
    references: list[dict[str, str]] | None = None,
    adopt_existing: bool = False,
    operation=None,
    initial_delivery=None,
) -> StreamingResponse:
    model = config.model
    stream = _stream(
        state,
        prompt,
        application_builder=chat_routes.build_local_application,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        source_node_id=source_id,
        adopt_existing=adopt_existing,
        mode=config.running_mode or "agent",
        permission_mode=config.permission_mode or "read_only",
        reasoning_effort=model.reasoning_effort if model is not None else "medium",
        provider_name=config.provider_name,
        model_snapshot=model.model_dump() if model is not None else None,
        request_model=model,
        user_preferences=state.agent_preferences(),
        model_config=_model_config_snapshot(state),
        references=references,
        operation=operation,
        initial_delivery=initial_delivery,
    )
    return StreamingResponse(stream, media_type="text/event-stream")


__all__ = ["_queue_http_error", "_references", "_stream_turn", "_turn", "_user_item"]
