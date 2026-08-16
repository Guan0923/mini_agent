"""HTTP/SSE helpers for the canonical node lifecycle protocol."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from backend.domain.runtime_state import NodeFrame, RuntimeState, RuntimeStateValidationError


def encode_node_frame(frame: NodeFrame) -> dict[str, Any]:
    """Return a JSON-safe lifecycle object with no legacy event envelope."""

    return frame.to_dict()


def encode_node_sse(frame: NodeFrame) -> str:
    return f"data: {json.dumps(encode_node_frame(frame), ensure_ascii=False, separators=(',', ':'))}\n\n"


def node_frames(frames: Iterable[NodeFrame]) -> Iterator[str]:
    """Yield every lifecycle frame until the producer closes the stream.

    A run may seal several nodes (for example a user node, tool result, and
    assistant node).  A delete frame is a node-level terminal transition, not
    an SSE-level terminal marker; stopping at the first delete would silently
    drop the rest of the tree.
    """

    for frame in frames:
        yield encode_node_sse(frame)


def decode_node(value: dict[str, Any]) -> RuntimeState:
    try:
        return RuntimeState.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeStateValidationError("Invalid RuntimeState node payload.") from exc


__all__ = ["decode_node", "encode_node_frame", "encode_node_sse", "node_frames"]
