"""Chat and runtime-decision API routes."""

from collections.abc import Callable

from backend.runtime import build_application as _default_build_application

from . import routes as _routes
from .routes import (
    BatchChatRequest,
    BatchMessage,
    ChatRequest,
    ReasoningEffort,
    ResumeRequest,
    _event_payload,
    _model_config_snapshot,
    _reasoning_parameters,
    _truncate,
    router,
)

build_application: Callable[..., object] = _default_build_application


def _stream(*args: object, **kwargs: object):
    """Preserve the historical stream patch point for API integrations."""

    _routes.build_application = build_application
    return _routes._stream(*args, **kwargs)


__all__ = [
    "BatchChatRequest",
    "BatchMessage",
    "ChatRequest",
    "ReasoningEffort",
    "ResumeRequest",
    "_event_payload",
    "_model_config_snapshot",
    "_reasoning_parameters",
    "_stream",
    "_truncate",
    "build_application",
    "router",
]
