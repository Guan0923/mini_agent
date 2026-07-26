"""Minimal readable-text extraction for fetched HTML."""

from __future__ import annotations

from html.parser import HTMLParser

from .text import normalize_whitespace


class ReadableHtmlParser(HTMLParser):
    """Small dependency-free extractor for static HTML content."""

    _IGNORED_TAGS = {"canvas", "footer", "form", "iframe", "nav", "noscript", "script", "style", "svg"}
    _BLOCK_TAGS = {"article", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "main", "p", "section", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    @property
    def title(self) -> str | None:
        title = normalize_whitespace(" ".join(self._title_parts))
        return title or None

    @property
    def text(self) -> str:
        return normalize_whitespace(" ".join(self._text_parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._title_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._title_depth = max(0, self._title_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._title_depth:
            self._title_parts.append(data)
        else:
            self._text_parts.append(data)
