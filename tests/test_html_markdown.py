import pytest

from mini_agent.tools import ToolError
from mini_agent.tools.html_markdown import extract_html_document


def extract(html: str, *, base_url: str = "https://example.com/docs/page"):
    return extract_html_document(html.encode(), base_url=base_url, declared_encoding="utf-8")


def test_extracts_title_headings_paragraphs_and_line_breaks() -> None:
    document = extract(
        "<html><head><title>  Example\n title </title></head><body><h1>Heading</h1><p>First<br>Second</p></body></html>"
    )

    assert document.title == "Example title"
    assert document.markdown == "# Heading\n\nFirst\\\nSecond"


def test_converts_ordered_unordered_and_nested_lists() -> None:
    document = extract(
        "<body><ul><li>Alpha<ul><li>Nested</li></ul></li><li>Beta</li></ul>"
        "<ol><li>First</li><li>Second</li></ol></body>"
    )

    assert document.markdown == "- Alpha\n  - Nested\n- Beta\n\n1. First\n2. Second"


def test_converts_quotes_emphasis_and_inline_code() -> None:
    document = extract(
        "<body><blockquote>Quoted</blockquote><p><strong>Bold</strong> <em>italic</em> <code>x()</code></p></body>"
    )

    assert document.markdown == "> Quoted\n\n**Bold** *italic* `x()`"


def test_preserves_fenced_code_whitespace_and_language() -> None:
    document = extract('<body><pre><code class="language-python">\n  def f():\n    return 1\n\n</code></pre></body>')

    assert document.markdown == "```python\n\n  def f():\n    return 1\n\n\n```"


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        (
            "<table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table>",
            "| Name | Value |\n| --- | --- |\n| A | 1 |",
        ),
        (
            "<table><tr><td>Name</td><td>Value</td></tr><tr><td>A</td><td>1</td></tr></table>",
            "| Name | Value |\n| --- | --- |\n| A | 1 |",
        ),
    ],
)
def test_converts_tables_and_infers_a_missing_header(table: str, expected: str) -> None:
    assert extract(f"<body>{table}</body>").markdown == expected


def test_normalises_safe_links_and_removes_unsafe_link_targets() -> None:
    document = extract(
        '<body><a href="../guide?q=1">Guide</a> '
        '<a href="javascript:alert(1)">JS</a> '
        '<a href="data:text/plain,no">Data</a> '
        '<a href="file:///tmp/a">File</a> '
        '<a href="mailto:a@example.com">Mail</a></body>'
    )

    assert document.markdown == "[Guide](https://example.com/guide?q=1) JS Data File Mail"


def test_keeps_only_non_empty_image_alt_text() -> None:
    document = extract('<body>Before <img src="x" alt="Diagram"> <img src="y" alt=""> after</body>')

    assert document.markdown == "Before Diagram after"


def test_removes_active_content_but_keeps_visible_container_text() -> None:
    document = extract(
        "<body><script>bad()</script><style>.bad{}</style><template>hidden</template>"
        "<svg>vector</svg><canvas>pixels</canvas><iframe>frame</iframe>"
        "<nav>Navigation</nav><footer>Footer</footer><noscript>No script</noscript><form>Form text</form></body>"
    )

    assert document.markdown == "Navigation\n\nFooter\n\nNo script\n\nForm text"


def test_malformed_html_produces_stable_markdown() -> None:
    document = extract("<title>Broken</title><body><h2>Heading</h2><p>Paragraph <strong>bold")

    assert document.title == "Broken"
    assert document.markdown == "## Heading\n\nParagraph **bold**"


def test_conversion_errors_are_wrapped_as_tool_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        del args, kwargs
        raise RecursionError("implementation detail")

    monkeypatch.setattr("mini_agent.tools.html_markdown.BeautifulSoup", fail)

    with pytest.raises(ToolError, match="convert HTML"):
        extract("<p>text</p>")
