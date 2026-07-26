"""Terminal-friendly LaTeX rendering for Textual Markdown widgets."""

from __future__ import annotations

import re
from collections.abc import Iterable

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.texmath import texmath_plugin
from pylatexenc.latex2text import LatexNodes2Text, get_default_latex_context_db
from pylatexenc.latexwalker import LatexEnvironmentNode, LatexMacroNode, LatexWalker
from textual.content import Content
from textual.widgets import Markdown
from textual.widgets.markdown import MarkdownBlock

_SUPERSCRIPTS = str.maketrans("0123456789+-=()in", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ")
_SUBSCRIPTS = str.maketrans("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
_SCRIPT_PATTERN = re.compile(r"(?<!\\)([\^_])(?:\{([^{}\n]+)\}|([A-Za-z0-9+\-=()]))")
_LATEX_TEXT_CONTEXT = get_default_latex_context_db()
_LATEX_TO_TEXT = LatexNodes2Text(latex_context=_LATEX_TEXT_CONTEXT)
_MATRIX_ENVIRONMENTS = frozenset({"array", "matrix", "pmatrix", "bmatrix", "vmatrix", "Vmatrix", "smallmatrix"})


def _walk_nodes(nodes: Iterable[object]) -> Iterable[object]:
    for node in nodes:
        yield node
        children = getattr(node, "nodelist", None)
        if children:
            yield from _walk_nodes(children)
        arguments = getattr(node, "nodeargd", None)
        if arguments is not None:
            yield from _walk_nodes(child for child in arguments.argnlist if child is not None)


def _has_unknown_construct(nodes: Iterable[object]) -> bool:
    for node in _walk_nodes(nodes):
        if isinstance(node, LatexMacroNode):
            if _LATEX_TEXT_CONTEXT.get_macro_spec(node.macroname) is None:
                return True
        elif isinstance(node, LatexEnvironmentNode):
            if (
                node.environmentname not in _MATRIX_ENVIRONMENTS
                and _LATEX_TEXT_CONTEXT.get_environment_spec(node.environmentname) is None
            ):
                return True
    return False


def _unicode_scripts(source: str) -> str:
    def replace(match: re.Match[str]) -> str:
        marker = match.group(1)
        value = match.group(2) or match.group(3) or ""
        table = _SUPERSCRIPTS if marker == "^" else _SUBSCRIPTS
        converted = value.translate(table)
        return converted if all(ord(character) in table for character in value) else match.group(0)

    return _SCRIPT_PATTERN.sub(replace, source)


def latex_to_terminal_text(source: str) -> str:
    """Convert a LaTeX formula to terminal text, or preserve it on failure."""

    try:
        nodes, _, _ = LatexWalker(source, tolerant_parsing=False).get_latex_nodes()
        if _has_unknown_construct(nodes):
            return source
        rendered = _LATEX_TO_TEXT.latex_to_text(_unicode_scripts(source)).strip()
    except Exception:
        return source
    return rendered or source


def latex_markdown_parser() -> MarkdownIt:
    """Build Textual's normal GFM parser with common math delimiters."""

    parser = MarkdownIt("gfm-like")
    parser.use(texmath_plugin, delimiters="dollars")
    parser.use(texmath_plugin, delimiters="brackets")

    def render_inline_math(state: object) -> None:
        for token in getattr(state, "tokens", ()):
            for child in token.children or ():
                if child.type in {"math_inline", "math_single"}:
                    child.type = "text"
                    child.content = latex_to_terminal_text(child.content)

    parser.core.ruler.after("inline", "terminal_math", render_inline_math)
    return parser


class LatexMathBlock(MarkdownBlock):
    """A selectable terminal representation of a display equation."""

    DEFAULT_CSS = """
    LatexMathBlock {
        height: auto;
        margin: 1 2;
        color: $text-accent;
    }
    """

    def __init__(self, markdown: Markdown, token: Token) -> None:
        super().__init__(markdown, token)
        self.set_content(Content(latex_to_terminal_text(token.content)))


class LatexMarkdown(Markdown):
    """Markdown widget that renders LaTeX as selectable terminal text."""

    def __init__(self, markdown: str | None = None, **kwargs: object) -> None:
        super().__init__(markdown, parser_factory=latex_markdown_parser, **kwargs)

    def unhandled_token(self, token: Token) -> MarkdownBlock | None:
        if token.type == "math_block":
            return LatexMathBlock(self, token)
        return super().unhandled_token(token)
