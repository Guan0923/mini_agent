"""Convert static HTML documents into bounded, readable Markdown."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import MarkdownConverter

from .base import ToolError

_REMOVED_TAGS = ("script", "style", "template", "svg", "canvas", "iframe")
_LANGUAGE_CLASS = re.compile(r"^language-(.+)$", flags=re.IGNORECASE)


@dataclass(frozen=True)
class HtmlDocument:
    """Readable metadata and Markdown extracted from one HTML response."""

    title: str | None
    markdown: str


def extract_html_document(
    body: bytes,
    *,
    base_url: str,
    declared_encoding: str | None,
) -> HtmlDocument:
    """Parse HTML bytes and convert the visible document to structured Markdown."""
    try:
        soup = BeautifulSoup(body, "html.parser", from_encoding=declared_encoding)
        title_element = soup.find("title")
        title = _normalise_text(title_element.get_text(" ", strip=True)) if title_element else ""

        for element in soup.find_all(_REMOVED_TAGS):
            element.decompose()
        _normalise_links(soup, base_url)
        _replace_images(soup)

        root = soup.body if soup.body is not None else soup
        markdown = _DocumentMarkdownConverter(
            heading_style="ATX",
            bullets="-",
            code_language_callback=_code_language,
            newline_style="backslash",
            strip_pre=None,
            table_infer_header=True,
        ).convert_soup(root)
        return HtmlDocument(title=title or None, markdown=_normalise_markdown(markdown))
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError("Unable to convert HTML content to Markdown.") from exc


def _normalise_links(soup: BeautifulSoup, base_url: str) -> None:
    for link in soup.find_all("a"):
        href = link.get("href")
        if not isinstance(href, str):
            link.unwrap()
            continue
        try:
            target = urljoin(base_url, href.strip())
            if urlsplit(target).scheme.lower() not in {"http", "https"}:
                link.unwrap()
                continue
        except ValueError:
            link.unwrap()
            continue
        link["href"] = target


class _DocumentMarkdownConverter(MarkdownConverter):
    """Treat visible page containers as blocks while using markdownify defaults elsewhere."""

    def convert_nav(self, element: Tag, text: str, parent_tags: set[str]) -> str:
        del element, parent_tags
        return _as_block(text)

    def convert_footer(self, element: Tag, text: str, parent_tags: set[str]) -> str:
        del element, parent_tags
        return _as_block(text)

    def convert_noscript(self, element: Tag, text: str, parent_tags: set[str]) -> str:
        del element, parent_tags
        return _as_block(text)

    def convert_form(self, element: Tag, text: str, parent_tags: set[str]) -> str:
        del element, parent_tags
        return _as_block(text)


def _replace_images(soup: BeautifulSoup) -> None:
    for image in soup.find_all("img"):
        alt = image.get("alt")
        text = _normalise_text(alt) if isinstance(alt, str) else ""
        if text:
            image.replace_with(NavigableString(text))
        else:
            image.decompose()


def _code_language(element: Tag) -> str:
    candidates = [element]
    code = element.find("code")
    if isinstance(code, Tag):
        candidates.append(code)
    for candidate in candidates:
        classes = candidate.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        for class_name in classes:
            match = _LANGUAGE_CLASS.match(str(class_name))
            if match:
                return match.group(1)
    return ""


def _normalise_markdown(value: str) -> str:
    lines = value.splitlines()
    output: list[str] = []
    in_fence = False
    blank_pending = False

    for line in lines:
        stripped = line.lstrip()
        is_fence = stripped.startswith("```") or stripped.startswith("~~~")
        if in_fence:
            output.append(line)
            if is_fence:
                in_fence = False
            continue
        if is_fence:
            if blank_pending and output:
                output.append("")
            blank_pending = False
            output.append(line.strip())
            in_fence = True
            continue
        if not line.strip():
            blank_pending = bool(output)
            continue
        if blank_pending and output:
            output.append("")
        blank_pending = False
        indent = line[: len(line) - len(stripped)]
        output.append(indent + re.sub(r"[ \t]+", " ", stripped).strip())

    return "\n".join(output).strip()


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _as_block(value: str) -> str:
    return f"\n\n{value.strip()}\n\n" if value.strip() else ""
