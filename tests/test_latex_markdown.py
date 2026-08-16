from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from tui.latex import LatexMarkdown, latex_markdown_parser, latex_to_terminal_text
from tui.rendering.transcript import MarkdownBody
from tui.screens.history import HistoryScreen
from tui.view import TerminalView


class _MarkdownApp(App[None]):
    def __init__(self, markdown: LatexMarkdown) -> None:
        super().__init__()
        self.markdown = markdown

    def compose(self) -> ComposeResult:
        yield self.markdown


async def _select_all(markdown: LatexMarkdown) -> str:
    app = _MarkdownApp(markdown)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen._select_all_in_widget(markdown)
        await pilot.pause()
        return app.screen.get_selected_text() or ""


def test_latex_converter_handles_common_math_and_safe_fallbacks() -> None:
    assert latex_to_terminal_text(r"\alpha + \beta \le \infty") == "α+ β≤∞"
    assert latex_to_terminal_text("x^2 + y_1") == "x² + y₁"
    assert latex_to_terminal_text(r"\frac{a}{b}") == "a/b"
    assert latex_to_terminal_text(r"\sqrt{x}") == "√(x)"
    assert latex_to_terminal_text(r"\begin{matrix}a&b\\c&d\end{matrix}") == "a   b\nc   d"
    assert latex_to_terminal_text(r"\unknown{x}") == r"\unknown{x}"
    assert latex_to_terminal_text("bad{formula") == "bad{formula"


def test_parser_supports_all_delimiters_without_touching_currency_or_code() -> None:
    tokens = latex_markdown_parser().parse(
        "Inline $x^2$ and \\(\\alpha+1\\).\n\n"
        "$$\\frac{a}{b}$$\n\n\\[y_1\\]\n\n"
        r"Cost $12.50 and \$5; code: `$z_2$`."
    )
    inline_text = [child.content for token in tokens for child in token.children or ()]
    blocks = [token.content for token in tokens if token.type == "math_block"]

    assert inline_text == ["Inline x² and α+1.", "Cost $12.50 and $5; code: ", "$z_2$", "."]
    assert blocks == [r"\frac{a}{b}", "y_1"]


def test_latex_markdown_renders_inline_and_block_math_as_selectable_text() -> None:
    async def scenario() -> None:
        selected = await _select_all(LatexMarkdown("Inline $x^2$ and \\(\\alpha\\).\n\n$$\\frac{a}{b}$$\n\n\\[y_1\\]"))
        assert "Inline x² and α." in selected
        assert "a/b" in selected
        assert "y₁" in selected

    asyncio.run(scenario())


def test_streaming_markdown_keeps_raw_source_and_renders_when_formula_closes() -> None:
    async def scenario() -> None:
        body = MarkdownBody("value $x")
        app = _MarkdownApp(body)
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            app.screen._select_all_in_widget(body)
            assert app.screen.get_selected_text() == "value $x"
            app.screen.clear_selection()

            body.set_markdown("value $x^2$")
            for _ in range(20):
                await pilot.pause()
                if not body._render_scheduled and not body._render_running:
                    break
            app.screen._select_all_in_widget(body)
            assert app.screen.get_selected_text() == "value x²"
            assert body.markdown_text == "value $x^2$"

    asyncio.run(scenario())


def test_all_markdown_entry_points_use_shared_renderer() -> None:
    history = HistoryScreen("session", [{"role": "assistant", "content": "$x^2$"}])
    assert any(isinstance(widget, LatexMarkdown) for widget in history._content_widgets())
    assert issubclass(MarkdownBody, LatexMarkdown)
    assert isinstance(TerminalView().review_details, LatexMarkdown)


def test_history_copy_uses_displayed_unicode_math() -> None:
    async def scenario() -> None:
        app = App[None]()
        copied: list[str] = []
        app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
        screen = HistoryScreen("session", [{"role": "assistant", "content": "$x^2$"}])
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            screen._select_all_in_widget(screen.history_log)
            selected = screen.get_selected_text()
            assert selected == "ASSISTANT\nx²"

            await pilot.click("#history-log", button=3)
            await pilot.pause()
            assert copied == [selected]

    asyncio.run(scenario())
