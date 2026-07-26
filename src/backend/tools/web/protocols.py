"""Small protocols used to inject web dependencies in tests."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

DdgrRunner = Callable[..., subprocess.CompletedProcess[str]]
HostResolver = Callable[..., list[tuple[Any, ...]]]


class HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]
    encoding: str | None

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False) -> Iterable[bytes]: ...

    def close(self) -> None: ...


class HttpSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...
