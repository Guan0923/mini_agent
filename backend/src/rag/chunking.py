"""Page-aware PDF chunking and Chinese FTS5 preprocessing."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .models import KnowledgeChunk, PdfPage

try:  # jieba is optional so the PDF feature remains usable in a minimal install.
    import jieba
except ImportError:  # pragma: no cover - exercised when optional dependency is absent
    jieba = None


_SENTENCE_END = re.compile(r"[。！？；.!?;:]\s*$")
_HEADING = re.compile(r"^(?:第[一二三四五六七八九十百]+[章节条]|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+)*[、.])")
_WORD = re.compile(r"[\u4e00-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?|\S")


def tokenize_text(text: str) -> list[str]:
    """Return stable word tokens, preferring jieba for Chinese text."""

    if jieba is not None:
        return [token for token in jieba.cut(text, cut_all=False) if token.strip()]
    return _WORD.findall(text)


def pretokenize(text: str) -> str:
    """Insert spaces between tokens before writing to SQLite FTS5."""

    return " ".join(tokenize_text(text))


def token_count(text: str) -> int:
    return len(tokenize_text(text))


@dataclass(frozen=True)
class _Paragraph:
    text: str
    page_start: int
    page_end: int


def _clean_page_lines(page: PdfPage) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in page.text.splitlines()]
    return [line for line in lines if line]


def _remove_repeated_headers_footers(pages: list[PdfPage]) -> list[list[str]]:
    page_lines = [_clean_page_lines(page) for page in pages]
    counts: dict[str, int] = {}
    for lines in page_lines:
        for candidate in {*(lines[:1]), *(lines[-1:])}:
            if candidate:
                counts[candidate] = counts.get(candidate, 0) + 1
    repeated = {line for line, count in counts.items() if count >= 2 and len(line) <= 160}
    return [[line for line in lines if line not in repeated] for lines in page_lines]


def _paragraphs(pages: Iterable[PdfPage]) -> list[_Paragraph]:
    page_list = list(pages)
    cleaned = _remove_repeated_headers_footers(page_list)
    paragraphs: list[_Paragraph] = []
    pending = ""
    pending_start = 0
    pending_end = 0
    for page, lines in zip(page_list, cleaned, strict=False):
        for line in lines:
            is_heading = bool(_HEADING.match(line))
            if is_heading and pending:
                paragraphs.append(_Paragraph(pending.strip(), pending_start, pending_end))
                pending = ""
            if not pending:
                pending_start = page.number
            join_with_previous = bool(pending) and not _SENTENCE_END.search(pending) and not is_heading
            pending = (
                f"{pending} {line}".strip()
                if join_with_previous
                else (f"{pending}\n{line}".strip() if pending else line)
            )
            pending_end = page.number
            # A blank line is already collapsed by extraction; a completed sentence
            # followed by another line is kept in the same paragraph unless a heading appears.
        if pending and pending_end < page.number:
            pending_end = page.number
    if pending:
        paragraphs.append(_Paragraph(pending.strip(), pending_start, pending_end))
    return paragraphs


def _window(tokens: list[str], start: int, end: int) -> str:
    return " ".join(tokens[start:end]).strip()


def chunk_pdf_pages(
    pages: Iterable[PdfPage],
    *,
    target_tokens: int = 700,
    overlap_tokens: int = 100,
) -> list[KnowledgeChunk]:
    """Chunk extracted pages while preserving page ranges and stable hashes."""

    if target_tokens < 1 or overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("target_tokens must be positive and overlap_tokens must be smaller")
    paragraphs = _paragraphs(pages)
    chunks: list[KnowledgeChunk] = []
    sequence = 0
    for paragraph in paragraphs:
        tokens = tokenize_text(paragraph.text)
        if not tokens:
            continue
        if len(tokens) <= target_tokens:
            windows = [(0, len(tokens))]
        else:
            windows = []
            step = target_tokens - overlap_tokens
            start = 0
            while start < len(tokens):
                end = min(start + target_tokens, len(tokens))
                windows.append((start, end))
                if end == len(tokens):
                    break
                start += step
        for start, end in windows:
            text = _window(tokens, start, end)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            chunk_id = hashlib.sha256(f"{digest}:{sequence}".encode()).hexdigest()[:32]
            chunks.append(
                KnowledgeChunk(
                    chunk_id=chunk_id,
                    document_id="",
                    section_id="",
                    text=text,
                    tokenized_text=pretokenize(text),
                    page_start=paragraph.page_start,
                    page_end=paragraph.page_end,
                    sequence=sequence,
                    token_count=len(tokens[start:end]),
                    sha256=digest,
                )
            )
            sequence += 1
    return chunks
