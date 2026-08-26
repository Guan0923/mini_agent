"""HTTP/SSE helpers for baseline Turn snapshots and incremental deltas."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from backend.domain.runtime_state import NodeFrame, RuntimeState, RuntimeStateValidationError


def encode_node_frame(frame: NodeFrame) -> dict[str, Any]:
    """Return a JSON-safe snapshot or delta with no RuntimeEvent envelope."""

    return frame.to_dict()


def encode_node_sse(frame: NodeFrame) -> str:
    return f"data: {json.dumps(encode_node_frame(frame), ensure_ascii=False, separators=(',', ':'))}\n\n"


def node_frames(frames: Iterable[NodeFrame]) -> Iterator[str]:
    """Yield every baseline/delta frame until the producer closes the stream."""

    for frame in frames:
        yield encode_node_sse(frame)


def decode_node(value: dict[str, Any]) -> RuntimeState:
    try:
        return RuntimeState.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeStateValidationError("Invalid RuntimeState node payload.") from exc


__all__ = ["decode_node", "encode_node_frame", "encode_node_sse", "node_frames"]
