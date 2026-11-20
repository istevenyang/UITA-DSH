"""独立 API TCS Lexer。

Lexer 只保留行号、缩进和原始文本；函数调用/JSON 的结构语义交给 Parser，避免与
UI core.dsl 的 action[...] 规则耦合。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApiSourceLine:
    number: int
    indent: int
    text: str


class ApiLexError(ValueError):
    pass


def lex(source: str) -> list[ApiSourceLine]:
    lines: list[ApiSourceLine] = []
    for number, raw in enumerate(source.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise ApiLexError(f"Line {number}: API TCS only supports space indentation")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(ApiSourceLine(number=number, indent=len(raw) - len(raw.lstrip(" ")), text=stripped))
    return lines
