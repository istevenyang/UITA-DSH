"""CaseContext — 跨步骤变量存储，支持 ${var} 占位符渲染。"""
from __future__ import annotations

import re
from typing import Optional


class CaseContext:
    """跨步骤变量存储。

    用法::

        ctx = CaseContext()
        ctx.set("reply", "你好，我是 AI")
        ctx.render("回复内容：${reply}")  # → "回复内容：你好，我是 AI"
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self._store[key.strip()] = value

    def get(self, key: str) -> Optional[str]:
        return self._store.get(key.strip())

    def render(self, text: str) -> str:
        """将 ${key} 替换为已存储值，未命中保留原文。"""
        if "${" not in text:
            return text

        def _replace(m: re.Match) -> str:
            key = m.group(1).strip()
            return self._store.get(key, m.group(0))

        return re.sub(r"\$\{([^}]+)\}", _replace, text)

    def all(self) -> dict[str, str]:
        return dict(self._store)

    def __repr__(self) -> str:
        return f"CaseContext({self._store})"
