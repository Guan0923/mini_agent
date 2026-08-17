"""Session-scoped file upload, search, preview, and deletion.

The store owns the durable upload directory below a session's workspace
(``runtime/<session>/workspace/uploads``) plus the read-only project search
root (the session's effective cwd, which may be an external project folder).
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
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from backend.configuration import ClientPaths
from backend.tools.filesystem.paths import workspace_relative_parts

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
        self._project_root = Path(project_root).resolve() if project_root is not None else None

    @property
    def session_id(self) -> str:
        return self._session_id

    def upload_root(self) -> Path:
        """Return the canonical upload directory, creating it if needed."""

        self._paths.ensure_session(self._session_id)
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
        stat = path.stat()
        return {
            "source": source,
            "path": path.relative_to(self._root_for(source)).as_posix(),
            "name": path.name,
            "size": stat.st_size,
            "mime": _mime_for(path.name),
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "is_image": is_image(path.name),
        }

    def _root_for(self, source: str) -> Path:
        if source == "upload":
            return self.upload_root()
        if source == "project":
            root = self.project_root()
            if root is None:
                raise SessionFileError("项目 cwd 不可访问，无法引用项目文件。")
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
        """Search project and upload roots with the file-tool ignore policy."""

        if not isinstance(limit, int) or limit < 1:
            raise SessionFileError("limit 必须是正整数。")
        limit = min(limit, MAX_SEARCH_RESULTS)
        query = (q or "").casefold()
        results: list[dict[str, object]] = []
        upload_root = self.upload_root()
        # For ordinary sessions the project root is the session workspace,
        # which contains the upload directory.  Skip that nested directory in
        # the project scan so uploaded files are never duplicated under a
        # second source label.
        skipped_in_project = {upload_root.name} if self._project_root == upload_root.parent else set()
        roots: list[tuple[Path, str, set[str]]] = []
        if self._project_root is not None:
            roots.append((self._project_root, "project", skipped_in_project))
        roots.append((upload_root, "upload", set()))
        for root, source, skipped in roots:
            if len(results) >= limit:
                break
            for path in self._iter_files(root, skipped):
                if len(results) >= limit:
                    break
                relative = path.relative_to(root).as_posix()
                if query and query not in relative.casefold() and query not in path.name.casefold():
                    continue
                results.append(self.metadata(path, source))
        return results[:limit]

    def _iter_files(self, root: Path, skipped_dirs: set[str] | None = None) -> Iterable[Path]:
        """Yield regular files inside ``root`` using the tool ignore policy.

        The walk never follows symbolic links, ignores the standard tool
        ignore directories, and stops after a bounded number of examined
        files so a huge project cannot stall the search endpoint.
        """

        ignored = _IGNORED_DIRECTORIES | (skipped_dirs or set())
        examined = 0
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directories[:] = sorted(
                name for name in directories if name not in ignored and not (directory_path / name).is_symlink()
            )
            for name in sorted(filenames):
                examined += 1
                if examined > MAX_WALKED_FILES:
                    return
                file_path = directory_path / name
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                resolved = file_path.resolve()
                if resolved == root or root not in resolved.parents:
                    continue
                yield resolved

    def resolve(self, source: str, path: str) -> Path:
        """Resolve a confined reference to a real regular file."""

        root = self._root_for(source)
        try:
            parts = workspace_relative_parts(path)
        except Exception as exc:
            raise SessionFileError(f"无效的文件路径：{path}") from exc
        candidate = root.joinpath(*parts)
        if candidate.is_symlink():
            raise SessionFileError("不允许引用符号链接文件。")
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise SessionFileError("文件路径超出允许范围。")
        if not resolved.is_file():
            raise SessionFileError("引用的文件不存在。")
        return resolved

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
