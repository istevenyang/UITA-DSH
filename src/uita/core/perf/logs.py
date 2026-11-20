"""把应用日志目录打包成 zip（纯标准库，应用无关、平台无关）。

用途（2026-10-12 用户要求）：压测场景（``apps/<app>/perf/scenarios/stress.py``）把"示例媒体应用
被压到进程消失"时刻的应用日志（macOS ``~/Library/Logs/DemoGallery/``、
Windows ``%APPDATA%\\DemoGallery\\logs\\``）打成
``${测试标题}_${测试次数}_${时间戳}.zip`` 随报告目录一起归档——**日志路径是应用知识**，
由 app 侧提供，本模块只负责"给一组目录 → 产出一个 zip"。

设计约束：
  - 纯 stdlib（``zipfile``/``os``），不引入新依赖（R13）；
  - **不产出空 zip**：一个文件都没打包到就删掉 zip 并返回 ``created=False`` + ``reason``，
    否则下游看到"有个 zip"会误以为日志已归档；
  - 目录不存在只记 ``missing`` 不报错（"这个平台/版本没有日志目录"是常态）；
  - ``arcname`` 用 ``<目录名>/<相对路径>``，多目录打包时不会互相覆盖同名文件。
"""
from __future__ import annotations

import logging
import os
import zipfile
from typing import Any, Iterable, Optional

logger = logging.getLogger("uita")

__all__ = ["zip_dirs"]


def zip_dirs(
    sources: Iterable[str],
    zip_path: str,
    *,
    compress: bool = True,
) -> dict[str, Any]:
    """把若干目录（递归）打包成 ``zip_path``；返回打包结果明细。

    Args:
        sources: 待打包目录列表（不存在的目录记入 ``missing``，不报错）。
        zip_path: 目标 zip 路径（父目录会自动创建）。
        compress: ``True``（默认）用 ``ZIP_DEFLATED``；``False`` 用 ``ZIP_STORED``
            （日志多为文本，压缩率可观；调试时可关掉以便直接读）。

    Returns:
        ``{"created": bool, "zip_path": str, "files": int, "bytes": int,
        "sources": [...], "missing": [...], "errors": [...], "reason": str}``

        - ``created=False`` 时 ``reason`` 说明原因（目录都不存在 / 目录内无可打包文件）；
        - ``bytes`` 是**原始**字节数合计（不是 zip 体积），便于判断"日志到底有多少量"。
    """
    srcs = [str(s).strip() for s in (sources or []) if str(s or "").strip()]
    present = [s for s in srcs if os.path.isdir(s)]
    missing = [s for s in srcs if s not in present]
    result: dict[str, Any] = {
        "created": False,
        "zip_path": "",
        "files": 0,
        "bytes": 0,
        "sources": present,
        "missing": missing,
        "errors": [],
        "reason": "",
    }
    if not present:
        result["reason"] = "日志目录不存在（无可打包内容）" if srcs else "未提供日志目录"
        return result

    parent = os.path.dirname(os.path.abspath(zip_path))
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        result["reason"] = f"创建目录失败: {type(exc).__name__}: {exc}"
        return result

    files = 0
    total = 0
    errors: list[str] = []
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    try:
        with zipfile.ZipFile(zip_path, "w", mode) as zf:
            for src in present:
                base = os.path.basename(os.path.normpath(src)) or "logs"
                for root, _dirs, names in os.walk(src):
                    for name in sorted(names):
                        full = os.path.join(root, name)
                        rel = os.path.relpath(full, src)
                        try:
                            # 软链/目录跳过：日志目录里出现软链会打包成"另一个文件的副本"，
                            # 让日志量看起来虚高（宁缺勿错）
                            if os.path.islink(full) or not os.path.isfile(full):
                                continue
                            zf.write(full, arcname=os.path.join(base, rel))
                            files += 1
                            total += os.path.getsize(full)
                        except OSError as exc:  # noqa: PERF203 - 单个文件失败不放弃整批
                            errors.append(f"{full}: {type(exc).__name__}: {exc}")
    except (OSError, zipfile.BadZipFile) as exc:
        result["errors"] = errors
        result["reason"] = f"写入 zip 失败: {type(exc).__name__}: {exc}"
        return result

    if files == 0:
        # 不产出空 zip：删掉半成品，如实说明原因（否则下游看到 zip 会误以为日志已归档）
        _remove_quietly(zip_path)
        result["errors"] = errors
        result["reason"] = "日志目录内没有可打包文件"
        return result

    result.update(created=True, zip_path=zip_path, files=files, bytes=total, errors=errors)
    logger.info("[perf] 日志已打包: %s（%d 个文件，%.1fKB）", zip_path, files, total / 1024.0)
    return result


def _remove_quietly(path: str) -> None:
    """删除文件，失败只记 debug（清理失败不该影响主流程）。"""
    try:
        os.remove(path)
    except OSError as exc:  # noqa: BLE001
        logger.debug("[perf] 删除临时 zip 失败: %s（%s）", path, exc)


def default_zip_name(tier: str, round_index: Optional[int], timestamp: str) -> str:
    """``${测试标题}_${测试次数}_${时间戳}.zip``（如 ``50K_2_20261012114505.zip``）。

    ``tier`` 为空记 ``unknown``；``round_index`` 为空/0 记 ``0``——命名永远可解析，
    避免产出 ``__20261012114505.zip`` 这种没法追溯是哪个档位/轮次的文件名。
    """
    label = str(tier or "").strip() or "unknown"
    idx = int(round_index) if isinstance(round_index, int) and round_index > 0 else 0
    return f"{label}_{idx}_{str(timestamp or '').strip()}.zip"
