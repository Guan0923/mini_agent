"""A small extraction of the Requests method-normalization path.

The implementation intentionally contains the historical bytes-method bug
from SWE-bench instance psf__requests-2317.
"""

from __future__ import annotations


def builtin_str(value: object) -> str:
    return str(value)


class PreparedRequest:
    def __init__(self) -> None:
        self.method: str | None = None

    def prepare_method(self, method: object) -> None:
        self.method = builtin_str(method).upper()
