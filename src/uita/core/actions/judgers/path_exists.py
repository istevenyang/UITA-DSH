"""
PathExistsJudger — if[]/elif[]（及共用同一派发逻辑的 assert[]/verify[]）裸括号
本地文件/路径存在性判定（2026-07-25 新增）。

裸写法（无任何 kwarg）默认走 ExistenceJudger（当作"元素描述"去 resolve UI 元素）。
本模块新增 is_path_like() 启发式：当裸括号内容命中"路径特征"时，改为走本判定器，
检查本地文件系统路径是否存在，而非尝试 resolve 一个 UI 元素。

见 docs/plans/archive/02-dsl-actions-实施纪要.md。
"""
from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Optional

from uita.core.actions.common._file_path_helper import resolve_relative_path

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult
    from uita.core.runner.case_context import CaseContext

_RE_ABS_UNIX = re.compile(r"^/")
_RE_HOME = re.compile(r"^~")
_RE_REL_DOT = re.compile(r"^\.\.?/")
_RE_WIN_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")
_RE_PURE_VAR = re.compile(r"^\$\{[^}]+\}$")


def is_path_like(text: str) -> bool:
    """裸括号内容（无 kwarg 时的 description）是否应判定为"路径"而非"元素描述"。

    命中任一即为路径：
      - 以 "/" 开头（Unix 绝对路径）
      - 以 "~" 开头（家目录展开，如 "~/Downloads/x.zip"）
      - 以 "./" 或 "../" 开头（显式相对路径）
      - 形如 "C:\\..." / "C:/..." （Windows 盘符绝对路径）
      - 整体恰好是单个 "${var}" 变量引用（无其他文本混杂）

    不满足以上任一 → 视为元素描述，走原有 ExistenceJudger 逻辑（零回归）。
    全量存量 421 个 .tcs 用例排查零命中该启发式，见计划文档 §2。
    """
    t = text.strip()
    if not t:
        return False
    return bool(
        _RE_ABS_UNIX.match(t)
        or _RE_HOME.match(t)
        or _RE_REL_DOT.match(t)
        or _RE_WIN_DRIVE.match(t)
        or _RE_PURE_VAR.match(t)
    )


def resolve_and_check_exists(
    raw: str, case_ctx: Optional["CaseContext"] = None
) -> tuple[bool, str]:
    """${var} 渲染 → resolve_relative_path(must_exist=False) → os.path.exists()。

    返回 (是否存在, 已解析绝对路径)。供 PathExistsJudger 与
    core/actions/common/action_exist.py::ExistAction 共用，避免两处各自实现一份
    路径解析 + 存在性判断逻辑（提升内聚性）。
    """
    rendered = case_ctx.render(raw) if case_ctx is not None else raw
    resolved = resolve_relative_path(rendered, must_exist=False)
    return os.path.exists(resolved), resolved


class PathExistsJudger(JudgerBase):
    name = "path_exists"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        case_ctx = getattr(ctx.engine, "_case_context", None)
        exists, resolved = resolve_and_check_exists(spec.description, case_ctx)
        if exists:
            return JudgeResult(ok=True, last_value=resolved)
        return JudgeResult(
            ok=False, reason=f"路径不存在: {resolved}（原始: {spec.description}）"
        )
