"""A small extraction of pytest's first-statement classification path."""

from __future__ import annotations

import ast


def first_statement_kind(source: str) -> str:
    module = ast.parse(source)
    if not module.body:
        return "empty"
    first = module.body[0]
    # Historical bug: every expression was treated as a docstring.
    if isinstance(first, ast.Expr):
        return "docstring"
    return "other"


def rewrite_assertions(source: str) -> str:
    """Placeholder for the assertion rewrite transformation."""
    return source
