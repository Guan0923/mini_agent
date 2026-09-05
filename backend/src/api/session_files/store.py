"""Session-scoped file upload, search, preview, and deletion.

The store owns the durable upload directory below a session's workspace
(``runtime/<session>/workspace/uploads``), searches the complete workspace,
and optionally searches the bound project directory.
All handlers here are workspace-confined and reject traversal, symbolic links
and special files.  Upload batches are staged to temporary files and renamed
atomically so a failed batch never leaves partial files behind.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from backend.configuration import ClientPaths, ConfigurationError
from backend.domain.file_paths import FILE_SOURCES, ScopedPaths

MAX_FILES_PER_BATCH = 20
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BATCH_BYTES = 200 * 1024 * 1024
MAX_SEARCH_RESULTS = 100
MAX_WALKED_FILES = 20_000
_CHUNK_SIZE = 1024 * 1024
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mini_agent",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico"})
_WINDOWS_RESERVED = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING_DOTS_SPACES = re.compile(r"[. ]+$")


class SessionFileError(ValueError):
    """A session file operation was rejected or failed."""


def _mime_for(name: str) -> str:
    guessed, _encoding = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def is_image(name: str) -> bool:
    return Path(name).suffix.casefold() in _IMAGE_EXTENSIONS or _mime_for(name).startswith("image/")


class SessionFileStore:
    """Upload/search/preview/delete operations for one session."""

    def __init__(self, paths: ClientPaths, session_id: str, project_root: Path | None = None) -> None:
        self._paths = paths
        self._session_id = session_id
        self._project_root = Path(project_root).absolute() if project_root is not None else None
        self.file_paths = ScopedPaths(paths.session_workspace(session_id), self._project_root)

    @property
    def session_id(self) -> str:
        return self._session_id

    def upload_root(self) -> Path:
        """Return the canonical upload directory, creating it if needed."""

        try:
            self._paths.ensure_session(self._session_id)
        except ConfigurationError as exc:
            raise SessionFileError("上传目录不可用。") from exc
        return self._paths.session_uploads(self._session_id)

    def project_root(self) -> Path | None:
        """Return the searchable project root, or ``None`` when unavailable."""

        return self._project_root

    @staticmethod
    def sanitize_name(name: str) -> str:
        """Reduce a client filename to a safe display name.

        Path separators, control characters, Windows-reserved characters and
        trailing dots/spaces are stripped; a fully sanitized name falls back
        to ``file``.  The cleaned name is never used as a filesystem path on
        its own — :meth:`unique_target` resolves it against the upload root.
        """

        if not isinstance(name, str):
            name = str(name or "")
        normalized = unicodedata.normalize("NFKC", name)
        cleaned = _WINDOWS_RESERVED.sub("_", normalized).strip()
        cleaned = _TRAILING_DOTS_SPACES.sub("", cleaned)
        cleaned = cleaned.strip(" .")
        if not cleaned or cleaned in {".", ".."}:
            return "file"
        return cleaned[:200]

    @staticmethod
    def unique_target(upload_root: Path, name: str) -> tuple[Path, str]:
        """Return ``(path, stored_name)`` avoiding collisions with ``name (2).ext``."""

        base = Path(name)
        stem = base.stem or "file"
        suffix = base.suffix
        candidate = upload_root / name
        counter = 2
        while candidate.exists() or candidate.is_symlink():
            candidate = upload_root / f"{stem} ({counter}){suffix}"
            counter += 1
        return candidate, candidate.name

    def metadata(self, path: Path, source: str) -> dict[str, object]:
        resolved = self.resolve(source, str(path.absolute()))
        scoped = self.file_paths.format(resolved, scope="project" if source == "project" else "workspace")
        stat = path.stat()
        return {
            "source": source,
            "path": scoped,
            "display_path": scoped,
            "name": path.name,
            "size": stat.st_size,
            "mime": _mime_for(path.name),
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "is_image": is_image(path.name),
        }

    def _root_for(self, source: str) -> Path:
        if source == "workspace":
            return self.file_paths.workspace
        if source == "upload":
            root = self.upload_root()
            if root.is_symlink():
                raise SessionFileError("上传目录不能是符号链接。")
            return root.resolve()
        if source == "project":
            root = self.project_root()
            if root is None:
                raise SessionFileError("当前会话没有项目目录。")
            return root
        raise SessionFileError(f"不支持的引用来源：{source}")

    def store_batch(self, items: Sequence[tuple[str, object]]) -> list[dict[str, object]]:
        """Stream a multipart batch into the upload directory atomically.

        ``items`` is a sequence of ``(client_filename, readable)`` pairs where
        ``readable`` exposes a synchronous ``read(size)``.  Every file is
        staged to a temporary sibling first; only after the whole batch
        validates are the temporary files renamed to their final names.  A
        failure anywhere removes all staged and already-renamed files, so a
        rejected batch never leaves partial uploads behind.
        """

        if len(items) > MAX_FILES_PER_BATCH:
            raise SessionFileError(f"单次最多上传 {MAX_FILES_PER_BATCH} 个文件。")
        upload_root = self.upload_root()
        staged: list[tuple[Path, str]] = []
        renamed: list[Path] = []
        current_temporary: Path | None = None
        total = 0
        try:
            for index, (client_name, readable) in enumerate(items):
                filename = self.sanitize_name(client_name or f"file-{index + 1}")
                temporary = upload_root / f".upload-{uuid4().hex}.tmp"
                current_temporary = temporary
                staged.append((temporary, filename))
                size = 0
                with temporary.open("wb") as handle:
                    while True:
                        chunk = readable.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        total += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise SessionFileError(f"单文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB 限制：{filename}")
                        if total > MAX_BATCH_BYTES:
                            raise SessionFileError(f"单次上传合计超过 {MAX_BATCH_BYTES // (1024 * 1024)} MiB 限制。")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                current_temporary = None
            for temporary, filename in staged:
                target, stored_name = self.unique_target(upload_root, filename)
                os.replace(temporary, target)
                renamed.append(target)
            return [self.metadata(path, "upload") for path in renamed]
        except Exception:
            if current_temporary is not None:
                current_temporary.unlink(missing_ok=True)
            for temporary, _filename in staged:
                temporary.unlink(missing_ok=True)
            for path in renamed:
                path.unlink(missing_ok=True)
            raise

    def search(self, q: str, limit: int = 20) -> list[dict[str, object]]:
        """Search both complete roots, retaining upload ownership labels."""

        if not isinstance(limit, int) or limit < 1:
            raise SessionFileError("limit 必须是正整数。")
        limit = min(limit, MAX_SEARCH_RESULTS)
        query = (q or "").strip().replace("\\", "/").casefold()
        scope, separator, relative_query = query.partition(":")
        search_scope = scope if separator and scope in {"workspace", "project"} else None
        if search_scope:
            query = relative_query
        results: list[dict[str, object]] = []
        upload_root = self.upload_root()
        roots: list[tuple[Path, str]] = []
        if self._project_root is not None and search_scope != "workspace":
            roots.append((self._project_root, "project"))
        if search_scope != "project":
            roots.append((self.file_paths.workspace, "workspace"))
        seen: set[Path] = set()
        for root, scope in roots:
            if len(results) >= limit:
                break
            for path in self._iter_files(root):
                if len(results) >= limit:
                    break
                relative = path.relative_to(root).as_posix()
                if query and query not in relative.casefold() and query not in path.name.casefold():
                    continue
                if path in seen:
                    continue
                seen.add(path)
                source = "upload" if path.is_relative_to(upload_root) else scope
                results.append(self.metadata(path, source))
        return results[:limit]

    def _iter_files(self, root: Path, skipped_dirs: set[str] | None = None) -> Iterable[Path]:
        """Yield regular files inside ``root`` using the tool ignore policy.

        The walk never follows symbolic links, ignores the standard tool
        ignore directories, and stops after a bounded number of examined
        files so a huge project cannot stall the search endpoint.
        """

        ignored = _IGNORED_DIRECTORIES | (skipped_dirs or set())
        try:
            ScopedPaths.reject_links(root, root)
        except ValueError as exc:
            raise SessionFileError(str(exc)) from exc
        examined = 0
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directories[:] = sorted(
                name for name in directories if name not in ignored and not self._is_link(directory_path / name)
            )
            for name in sorted(filenames):
                examined += 1
                if examined > MAX_WALKED_FILES:
                    return
                file_path = directory_path / name
                if self._is_link(file_path) or not file_path.is_file():
                    continue
                resolved = file_path.resolve()
                if resolved == root or root not in resolved.parents:
                    continue
                yield resolved

    @staticmethod
    def _is_link(path: Path) -> bool:
        try:
            ScopedPaths.reject_links(path, path)
        except ValueError:
            return True
        return False

    def resolve(self, source: str, path: str) -> Path:
        """Resolve a reference while checking both scope and source ownership."""

        root = self._root_for(source)
        try:
            prefix = path.partition(":")[0]
            expected_scope = "project" if source == "project" else "workspace"
            if prefix in {"workspace", "project"} and prefix != expected_scope:
                raise ValueError("文件路径前缀与来源不一致。")
            resolved = self.file_paths.resolve(path)
        except ValueError as exc:
            raise SessionFileError(str(exc)) from exc
        root = root.resolve()
        if resolved != root and root not in resolved.parents:
            raise SessionFileError("文件路径超出允许范围。")
        if not resolved.is_file():
            raise SessionFileError("引用的文件不存在。")
        return resolved

    def normalize_references(self, values: Sequence[Mapping[str, object]]) -> list[dict[str, str]]:
        """Validate browser references and return canonical scoped payloads."""

        references: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for value in values:
            source = value.get("source")
            path = value.get("path")
            if source not in FILE_SOURCES or not isinstance(path, str) or not path:
                raise SessionFileError("无效的文件引用。")
            resolved = self.resolve(str(source), path)
            canonical_source = (
                "upload" if resolved.is_relative_to(self._paths.session_uploads(self._session_id)) else str(source)
            )
            scoped = self.file_paths.format(resolved, scope="project" if canonical_source == "project" else "workspace")
            key = (canonical_source, scoped)
            if key in seen:
                continue
            seen.add(key)
            references.append(
                {
                    "source": key[0],
                    "path": key[1],
                    "display_path": scoped,
                }
            )
        return references

    def delete_upload(self, path: str) -> None:
        """Delete one uploaded file; project files are never deleted here."""

        resolved = self.resolve("upload", path)
        resolved.unlink()


def remove_upload_tree(upload_root: Path) -> None:
    """Remove a session's canonical upload directory without following links."""

    if upload_root.is_symlink():
        raise SessionFileError("上传目录不能是符号链接。")
    if upload_root.exists():
        if not upload_root.is_dir():
            raise SessionFileError("上传路径必须是目录。")
        for item in upload_root.rglob("*"):
            if item.is_symlink():
                raise SessionFileError(f"上传目录包含符号链接：{item.name}")
            if not item.is_file() and not item.is_dir():
                raise SessionFileError(f"上传目录包含特殊文件：{item.name}")
        shutil.rmtree(upload_root)


__all__ = [
    "MAX_BATCH_BYTES",
    "MAX_FILE_BYTES",
    "MAX_FILES_PER_BATCH",
    "MAX_SEARCH_RESULTS",
    "SessionFileError",
    "SessionFileStore",
    "is_image",
    "remove_upload_tree",
]
