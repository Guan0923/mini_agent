"""Compatibility facade for the historical chat route module."""

from collections.abc import Callable

from backend.runtime import build_application as _default_build_application

from .chat import routes as _routes
from .chat.routes import (
    ChatRequest,
    ReasoningEffort,
    ResumeRequest,
    _event_payload,
    _model_config_snapshot,
    _reasoning_parameters,
    _truncate,
    router,
)

# Keep the patch point used by older integrations and tests.  The actual
# implementation lives in ``api.chat.routes``; this wrapper copies an
# overridden factory into that module before creating a stream.
build_application: Callable[..., object] = _default_build_application


def _stream(*args: object, **kwargs: object):
    _routes.build_application = build_application
    return _routes._stream(*args, **kwargs)


__all__ = [
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
