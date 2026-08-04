"""A small extraction of Astropy's QDP command parser."""

from __future__ import annotations

_COMMANDS = {"READ", "TIME", "SKIP"}


def parse_commands(text: str) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        tokens = line.split()
        if tokens and tokens[0] in _COMMANDS:
            commands.append((tokens[0], tokens[1:]))
    return commands


def parse_data_rows(text: str) -> list[list[float | None]]:
    """Parse numeric QDP rows, retaining the upstream NO/nan conventions."""
    rows: list[list[float | None]] = []
    for line in text.splitlines():
        tokens = line.split()
        if not tokens or tokens[0].upper() in _COMMANDS:
            continue
        row: list[float | None] = []
        for token in tokens:
            if token.upper() == "NO":
                row.append(None)
            elif token.lower() == "nan":
                row.append(float("nan"))
            else:
                row.append(float(token))
        rows.append(row)
    return rows
