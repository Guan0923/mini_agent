"""Incremental plain-text mirror for the structured transcript."""

from __future__ import annotations

from collections.abc import Hashable, Iterator


class TranscriptTextMirror:
    """Track ordered transcript sections and materialize text only when read."""

    def __init__(self) -> None:
        self._owners: list[Hashable] = []
        self._titles: dict[Hashable, str | None] = {}
        self._bodies: dict[Hashable, list[Hashable]] = {}
        self._body_owners: dict[Hashable, Hashable] = {}
        self._body_text: dict[Hashable, str] = {}
        self._content_chars = 0
        self._section_count = 0
        self._revision = 0
        self._cached_revision = 0
        self._cached_text = ""
        self._materialization_count = 0

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def length(self) -> int:
        separators = max(0, self._section_count - 1)
        return self._content_chars + separators

    @property
    def materialization_count(self) -> int:
        """Expose materializations for deterministic performance tests."""

        return self._materialization_count

    @property
    def text(self) -> str:
        if self._cached_revision != self._revision:
            self._cached_text = "\n".join(self._sections())
            self._cached_revision = self._revision
            self._materialization_count += 1
        return self._cached_text

    def snapshot(self) -> tuple[int, str]:
        return self._revision, self.text

    def add_top_level(self, owner: Hashable, title: str | None) -> None:
        if owner in self._titles:
            raise ValueError("Transcript top level is already registered.")
        self._owners.append(owner)
        self._titles[owner] = title
        self._bodies[owner] = []
        if title is not None:
            self._add_section(title)
        self._changed()

    def add_body(self, owner: Hashable, body: Hashable, text: str) -> None:
        if owner not in self._titles:
            raise ValueError("Transcript top level is not registered.")
        if body in self._body_owners:
            raise ValueError("Transcript body is already registered.")
        self._bodies[owner].append(body)
        self._body_owners[body] = owner
        self._body_text[body] = text
        if text:
            self._add_section(text)
        self._changed()

    def update_body(self, body: Hashable, text: str) -> bool:
        old_text = self._body_text.get(body)
        if old_text is None and body not in self._body_owners:
            return False
        if old_text == text:
            return False
        if old_text:
            self._remove_section(old_text)
        self._body_text[body] = text
        if text:
            self._add_section(text)
        self._changed()
        return True

    def remove_top_level(self, owner: Hashable) -> None:
        if owner not in self._titles:
            return
        self._owners.remove(owner)
        title = self._titles.pop(owner)
        if title is not None:
            self._remove_section(title)
        for body in self._bodies.pop(owner):
            self._body_owners.pop(body, None)
            text = self._body_text.pop(body, "")
            if text:
                self._remove_section(text)
        self._changed()

    def clear(self) -> None:
        self._owners = []
        self._titles = {}
        self._bodies = {}
        self._body_owners = {}
        self._body_text = {}
        self._content_chars = 0
        self._section_count = 0
        self._changed()

    def _sections(self) -> Iterator[str]:
        for owner in self._owners:
            title = self._titles[owner]
            if title is not None:
                yield title
            for body in self._bodies[owner]:
                text = self._body_text[body]
                if text:
                    yield text

    def _add_section(self, text: str) -> None:
        self._content_chars += len(text)
        self._section_count += 1

    def _remove_section(self, text: str) -> None:
        self._content_chars -= len(text)
        self._section_count -= 1

    def _changed(self) -> None:
        self._revision += 1
