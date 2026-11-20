"""工作区文件与检索工具：供无头 DSH 会话读写/编辑/检索受控目录内文件。

安全边界（R5 / R9）：

- 路径必须位于允许根目录内（``WORKSPACE_DIR`` / ``REPORT_DIR`` / ``USER_DATA_DIR`` /
  ``ROOT_DIR`` 或会话目录 / ``ctx.workdir``），严禁 ``..`` 越界访问系统任意路径；
- 严禁读取或修改受保护配置文件（``.env.local``、``.env.secret``、``.mcp.json``、
  ``dsh_settings.json`` 及任何 ``.env*`` 凭据文件）；
- 写入统一采用同目录临时文件 + ``os.replace`` 原子替换。
"""
from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ..registry import (TIER_READ, TIER_WRITE, ArtifactRef, DshTool,
                        ToolResult, register_dsh_tool)

_PROTECTED_BASENAMES = frozenset({
    ".env",
    ".env.local",
    ".mcp.json",
    "dsh_settings.json",
})
_MAX_READ_CHARS = 32000
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules",
})


def _allowed_roots(ctx: Any) -> list[str]:
    from uita import config

    roots: list[str] = []
    for candidate in (
        getattr(ctx, "workdir", ""),
        getattr(getattr(ctx, "session", None), "root", ""),
        getattr(getattr(getattr(ctx, "session", None), "store", None), "session_dir", ""),
        getattr(config, "WORKSPACE_DIR", ""),
        getattr(config, "REPORT_DIR", ""),
        getattr(config, "USER_DATA_DIR", ""),
        getattr(config, "ROOT_DIR", ""),
    ):
        text = str(candidate or "").strip()
        if text:
            real = os.path.realpath(os.path.abspath(text))
            if real not in roots:
                roots.append(real)
    return roots


def _is_protected_path(real_path: str) -> bool:
    base = os.path.basename(real_path)
    if base in _PROTECTED_BASENAMES or base.startswith(".env."):
        return True
    norm = real_path.replace("\\", "/")
    if norm.endswith("/.env.secret") or "/.git/" in norm:
        return True
    return False


def resolve_safe_path(ctx: Any, raw_path: str, *, must_exist: bool = False) -> str:
    """校验并返回允许根目录内的绝对路径；越界或命中受保护配置抛 ``ValueError``。"""
    from uita import config

    text = str(raw_path or "").strip()
    if not text:
        raise ValueError("path 不能为空")
    base_dir = (
        str(getattr(ctx, "workdir", "") or "").strip()
        or str(getattr(config, "WORKSPACE_DIR", "") or "").strip()
        or os.getcwd()
    )
    candidate = text if os.path.isabs(text) else os.path.join(base_dir, text)
    real = os.path.realpath(os.path.abspath(candidate))
    if _is_protected_path(real):
        raise ValueError(f"受保护配置文件禁止访问（R5）: {raw_path}")

    roots = _allowed_roots(ctx)
    in_allowed = False
    for root in roots:
        try:
            if os.path.commonpath([root, real]) == root:
                in_allowed = True
                break
        except ValueError:
            continue
    if not in_allowed:
        raise ValueError(f"路径超出受控工作区范围: {raw_path}")
    if must_exist and not os.path.exists(real):
        raise ValueError(f"路径不存在: {raw_path}")
    return real


def _atomic_write_text(path: str, content: str) -> int:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    data = content.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".dsh_write.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(data)


@register_dsh_tool
class FileReadTool(DshTool):
    name = "file_read"
    tier = TIER_READ
    description = "读取工作区或报告目录内的文本文件（支持按行 offset/limit 分段读取）。"
    params = {
        "path": {"type": "string", "description": "文件相对路径或受控绝对路径"},
        "offset": {"type": "integer", "description": "起始行号（从 1 开始，默认 1）"},
        "limit": {"type": "integer", "description": "读取最大行数（默认 200）"},
    }
    required = ("path",)

    async def run(self, ctx: Any, path: str, offset: int = 1,
                  limit: int = 200) -> ToolResult:
        try:
            target = resolve_safe_path(ctx, path, must_exist=True)
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        if not os.path.isfile(target):
            return ToolResult.fail(f"不是普通文件: {path}")
        try:
            with open(target, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError as exc:
            return ToolResult.fail(f"读取失败: {exc}")

        start = max(1, int(offset or 1)) - 1
        max_lines = max(1, min(int(limit or 200), 1000))
        sliced = lines[start:start + max_lines]
        content = "".join(sliced)
        truncated = (start + max_lines < len(lines)) or (len(content) > _MAX_READ_CHARS)
        if len(content) > _MAX_READ_CHARS:
            content = content[:_MAX_READ_CHARS] + "\n... [truncated]"
        return ToolResult(ok=True, data={
            "path": target,
            "total_lines": len(lines),
            "offset": start + 1,
            "lines_returned": len(sliced),
            "truncated": truncated,
            "content": content,
        })


@register_dsh_tool
class FileWriteTool(DshTool):
    name = "file_write"
    tier = TIER_WRITE
    description = "将文本内容原子写入工作区或报告目录内的文件（严禁写受保护配置）。"
    params = {
        "path": {"type": "string", "description": "目标文件路径"},
        "content": {"type": "string", "description": "完整文本内容"},
    }
    required = ("path", "content")

    async def run(self, ctx: Any, path: str, content: str) -> ToolResult:
        try:
            target = resolve_safe_path(ctx, path, must_exist=False)
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        try:
            written = _atomic_write_text(target, str(content))
        except OSError as exc:
            return ToolResult.fail(f"写入失败: {exc}")
        ref = ArtifactRef(name=os.path.basename(target), path=target, kind="file")
        return ToolResult(
            ok=True,
            data={"path": target, "bytes_written": written},
            evidence=[ref],
        )


@register_dsh_tool
class FileEditTool(DshTool):
    name = "file_edit"
    tier = TIER_WRITE
    description = "对受控文件执行精确字符串替换（old_string → new_string）。"
    params = {
        "path": {"type": "string", "description": "目标文件路径"},
        "old_string": {"type": "string", "description": "待替换的原始文本片段"},
        "new_string": {"type": "string", "description": "替换后的新文本片段"},
        "replace_all": {"type": "boolean", "description": "是否替换全部命中（默认 false）"},
    }
    required = ("path", "old_string", "new_string")

    async def run(self, ctx: Any, path: str, old_string: str, new_string: str,
                  replace_all: bool = False) -> ToolResult:
        if not old_string:
            return ToolResult.fail("old_string 不能为空")
        try:
            target = resolve_safe_path(ctx, path, must_exist=True)
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        if not os.path.isfile(target):
            return ToolResult.fail(f"不是普通文件: {path}")
        try:
            with open(target, encoding="utf-8") as handle:
                original = handle.read()
        except OSError as exc:
            return ToolResult.fail(f"读取失败: {exc}")

        count = original.count(old_string)
        if count == 0:
            return ToolResult.fail("未找到匹配的 old_string")
        if count > 1 and not replace_all:
            return ToolResult.fail(
                f"old_string 在文件中出现 {count} 次，请补充上下文或设 replace_all=true"
            )
        updated = (
            original.replace(old_string, new_string)
            if replace_all
            else original.replace(old_string, new_string, 1)
        )
        try:
            written = _atomic_write_text(target, updated)
        except OSError as exc:
            return ToolResult.fail(f"写入失败: {exc}")
        replaced = count if replace_all else 1
        return ToolResult(ok=True, data={
            "path": target,
            "replacements": replaced,
            "bytes_written": written,
        })


@register_dsh_tool
class GlobSearchTool(DshTool):
    name = "glob_search"
    tier = TIER_READ
    description = "按 glob 模式在受控目录内检索文件路径（自动跳过 .git/.venv/__pycache__）。"
    params = {
        "pattern": {"type": "string", "description": "glob 模式（如 **/*.tcs 或 *.json）"},
        "path": {"type": "string", "description": "起始目录（缺省为工作区根目录）"},
        "limit": {"type": "integer", "description": "返回数量上限（默认 100）"},
    }
    required = ("pattern",)

    async def run(self, ctx: Any, pattern: str, path: str = ".",
                  limit: int = 100) -> ToolResult:
        try:
            root = resolve_safe_path(ctx, path or ".", must_exist=True)
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        if not os.path.isdir(root):
            return ToolResult.fail(f"不是目录: {path}")

        cap = max(1, min(int(limit or 100), 500))
        matches: list[str] = []
        norm_pat = pattern.strip()
        if not norm_pat:
            return ToolResult.fail("pattern 不能为空")

        root_path = Path(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                if _is_protected_path(full):
                    continue
                rel = os.path.relpath(full, root_path).replace("\\", "/")
                if fnmatch.fnmatch(rel, norm_pat) or fnmatch.fnmatch(fname, norm_pat):
                    matches.append(rel)
                    if len(matches) >= cap:
                        break
            if len(matches) >= cap:
                break
        return ToolResult(ok=True, data={
            "root": root,
            "pattern": norm_pat,
            "matches": sorted(matches),
            "count": len(matches),
            "truncated": len(matches) >= cap,
        })


@register_dsh_tool
class GrepSearchTool(DshTool):
    name = "grep_search"
    tier = TIER_READ
    description = "在受控文件或目录内用正则表达式搜索文本行。"
    params = {
        "pattern": {"type": "string", "description": "正则表达式"},
        "path": {"type": "string", "description": "目标文件或目录（缺省为工作区根）"},
        "glob": {"type": "string", "description": "文件名过滤（如 *.py 或 *.tcs）"},
        "limit": {"type": "integer", "description": "最大返回匹配条数（默认 50）"},
    }
    required = ("pattern",)

    async def run(self, ctx: Any, pattern: str, path: str = ".",
                  glob: str = "", limit: int = 50) -> ToolResult:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult.fail(f"正则非法: {exc}")
        try:
            target = resolve_safe_path(ctx, path or ".", must_exist=True)
        except ValueError as exc:
            return ToolResult.fail(str(exc))

        cap = max(1, min(int(limit or 50), 200))
        hits: list[dict[str, Any]] = []

        def _scan_file(file_path: str, display_rel: str) -> None:
            if _is_protected_path(file_path):
                return
            try:
                if os.path.getsize(file_path) > 2 * 1024 * 1024:
                    return
                with open(file_path, encoding="utf-8", errors="ignore") as handle:
                    for lineno, line in enumerate(handle, 1):
                        if regex.search(line):
                            hits.append({
                                "file": display_rel,
                                "line": lineno,
                                "text": line.rstrip("\r\n")[:300],
                            })
                            if len(hits) >= cap:
                                return
            except OSError:
                return

        if os.path.isfile(target):
            _scan_file(target, os.path.basename(target))
        else:
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for fname in filenames:
                    if glob and not fnmatch.fnmatch(fname, glob):
                        continue
                    full = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full, target).replace("\\", "/")
                    _scan_file(full, rel)
                    if len(hits) >= cap:
                        break
                if len(hits) >= cap:
                    break

        return ToolResult(ok=True, data={
            "pattern": pattern,
            "matches": hits,
            "count": len(hits),
            "truncated": len(hits) >= cap,
        })
