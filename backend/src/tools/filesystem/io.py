"""Atomic UTF-8 file I/O and text normalization helpers."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..base import ToolError


class FileIOMixin:
    def _exclusive_create(self, file_path: Path, content: str) -> None:
        opened = False
        completed = False
        try:
            with file_path.open("x", encoding="utf-8", newline="") as handle:
                opened = True
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            completed = True
        except FileExistsError as exc:
            raise ToolError(f"File already exists: {self._display_path(file_path)}") from exc
        except OSError as exc:
            raise ToolError(f"Unable to create {self._display_path(file_path)}: {exc}") from exc
        finally:
            if opened and not completed:
                file_path.unlink(missing_ok=True)

    def _atomic_replace(self, file_path: Path, content: str, *, expected_content: str) -> None:
        temporary_path: Path | None = None
        try:
            original_mode = stat.S_IMODE(file_path.stat().st_mode)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=file_path.parent,
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, original_mode)
            current = self._read_raw(file_path, self._display_path(file_path))
            if current != expected_content:
                raise ToolError(f"File changed during the operation: {self._display_path(file_path)}")
            os.replace(temporary_path, file_path)
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(f"Unable to replace {self._display_path(file_path)}: {exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _iter_text_chunks(self, file_path: Path, display_path: str) -> Iterator[str]:
        try:
            with file_path.open("r", encoding="utf-8", newline=None) as handle:
                while chunk := handle.read(self._TEXT_CHUNK_CHARS):
                    yield chunk
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_path}") from exc
        except OSError as exc:
            raise ToolError(f"Unable to read {display_path}: {exc}") from exc

    @staticmethod
    def _read_raw(file_path: Path, display_path: str) -> str:
        try:
            with file_path.open("r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_path}") from exc
        except OSError as exc:
            raise ToolError(f"Unable to read {display_path}: {exc}") from exc

    @staticmethod
    def _normalise_newlines(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @classmethod
    def _with_newline(cls, value: str, newline: str) -> str:
        return cls._normalise_newlines(value).replace("\n", newline)

    @staticmethod
    def _dominant_newline(value: str) -> str:
        crlf = value.count("\r\n")
        lf = value.count("\n") - crlf
        cr = value.count("\r") - crlf
        counts = [(crlf, "\r\n"), (lf, "\n"), (cr, "\r")]
        count, newline = max(counts, key=lambda item: item[0])
        return newline if count else "\n"

    def _display_path(self, path: Path) -> str:
        return path.resolve().as_posix()

    def _display_candidate(self, path: Path) -> str:
        try:
            return self._display_path(path)
        except ValueError:
            return str(path)

    @staticmethod
    def _validate_integer(name: str, value: Any, *, minimum: int, maximum: int | None = None) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{name} must be an integer.")
        if value < minimum or maximum is not None and value > maximum:
            if maximum is None:
                raise ToolError(f"{name} must be at least {minimum}.")
            raise ToolError(f"{name} must be between {minimum} and {maximum}.")
