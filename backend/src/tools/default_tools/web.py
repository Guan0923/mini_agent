"""Definitions for public web tools."""

from __future__ import annotations

from ..base import Tool
from ..web import DdgrWebSearch, SafeWebFetcher
from .schema import object_schema


def web_tools(search: DdgrWebSearch, fetcher: SafeWebFetcher) -> tuple[Tool, ...]:
    return (
        Tool(
            "web_search",
            "Searches the public web with DuckDuckGo and returns result titles, URLs, and available snippets.",
            search.search,
            object_schema(
                {
                    "query": {
                        "type": "string",
                        "description": "The web search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                        "description": ("The maximum number of search results to return, from 1 to 10. Defaults to 5."),
                    },
                },
                ["query"],
            ),
            requires_confirmation=True,
            retryable=True,
        ),
        Tool(
            "web_fetch",
            (
                "Fetches an HTTP or HTTPS resource and returns readable content from HTML, plain-text, or JSON "
                "responses."
            ),
            fetcher.fetch,
            object_schema(
                {
                    "url": {
                        "type": "string",
                        "description": "The HTTP or HTTPS URL to fetch.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100_000,
                        "default": 50_000,
                        "description": (
                            "The maximum number of content characters to return, from 1 to 100000. Defaults to 50000."
                        ),
                    },
                },
                ["url"],
            ),
            requires_confirmation=True,
            retryable=True,
        ),
    )
