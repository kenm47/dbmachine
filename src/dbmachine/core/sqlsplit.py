"""Split a SQL script into individual statements.

Postgres function bodies use ``$$``/``$tag$`` dollar-quoting and contain
semicolons, so a naive ``split(';')`` corrupts them. This splitter respects
dollar-quoted strings, single-quoted literals, and ``--`` line comments.
"""

from __future__ import annotations

import re

_DOLLAR_TAG = re.compile(r"\$[a-zA-Z_]*\$")


def split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(script)
    in_squote = False
    dollar_tag: str | None = None

    while i < n:
        ch = script[i]

        if dollar_tag is not None:
            if script.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if in_squote:
            buf.append(ch)
            if ch == "'":
                in_squote = False
            i += 1
            continue

        # line comment
        if ch == "-" and script.startswith("--", i):
            j = script.find("\n", i)
            if j == -1:
                i = n
            else:
                buf.append(script[i : j + 1])
                i = j + 1
            continue

        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue

        m = _DOLLAR_TAG.match(script, i)
        if m:
            dollar_tag = m.group(0)
            buf.append(dollar_tag)
            i += len(dollar_tag)
            continue

        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements
