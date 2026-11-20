"""
rosetta_map 版本路由：按声明的被测 App 版本自动选择匹配的 rosetta_map 文件。

计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md

用法：
    from uita.core.platform.rosetta_map_version_selector import select_rosetta_map
    path = select_rosetta_map("apps/sample_web/rosetta_map.json", "2.1.3")

版本化 map 文件约定：
    apps/<app>/rosetta_map.<tag>.json   ← 版本化文件（如 rosetta_map.v2.json）
    apps/<app>/rosetta_map.json         ← 默认（无版本声明时的兜底）

    版本化文件的 _meta 须包含 app_version_range 字段：
    {
        "_meta": {
            "version": "3.0",
            "app_version_range": ">=2.0.0 <3.0.0"
        }
    }

支持的版本范围运算符（PEP 440 子集）：>= <= > < == !=
多个条件空格分隔，语义为 AND：如 ">=2.0.0 <3.0.0"
"""
from __future__ import annotations

import glob
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 版本号解析与比较
# ─────────────────────────────────────────────

def _parse_version(v: str) -> Optional[tuple]:
    """解析版本字符串为整数元组，失败返回 None。

    支持：'2.1.3' → (2, 1, 3)、'v2.1' → (2, 1)、'2' → (2,)
    """
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split(".")[:4])
    except (ValueError, AttributeError):
        return None


def _version_matches(app_ver: tuple, range_str: str) -> bool:
    """判断 app_ver 是否满足 range_str 声明的版本范围。

    Args:
        app_ver:   已解析的版本元组，如 (2, 1, 3)
        range_str: 版本范围字符串，如 ">=2.0.0 <3.0.0"

    Returns:
        True 表示满足全部条件（AND）；range_str 为空视为"通用"返回 False
        （调用方用 False 表示该候选没有声明范围，需跳过）。
    """
    if not range_str or not range_str.strip():
        return False  # 无 app_version_range = 通用默认文件，不参与精确匹配

    _OPS = [(">=", 2), ("<=", 2), ("!=", 2), (">", 1), ("<", 1), ("==", 2)]
    for clause in range_str.split():
        clause = clause.strip()
        if not clause:
            continue
        matched_op = False
        for op, oplen in _OPS:
            if clause.startswith(op):
                matched_op = True
                bound = _parse_version(clause[oplen:])
                if bound is None:
                    logger.debug("[rosetta_version] 版本范围条件无法解析: %r", clause)
                    return False
                # 元组长度对齐（短的补 0）
                maxlen = max(len(app_ver), len(bound))
                av = app_ver + (0,) * (maxlen - len(app_ver))
                bv = bound   + (0,) * (maxlen - len(bound))
                result = {
                    ">=": av >= bv,
                    "<=": av <= bv,
                    ">":  av >  bv,
                    "<":  av <  bv,
                    "==": av == bv,
                    "!=": av != bv,
                }[op]
                if not result:
                    return False
                break
        if not matched_op:
            logger.debug("[rosetta_version] 无法识别的版本范围 token: %r", clause)
            return False
    return True


# ─────────────────────────────────────────────
# 版本路由主函数
# ─────────────────────────────────────────────

def select_rosetta_map(default_map_path: str, app_version: str) -> str:
    """在 default_map_path 同目录下自动选择与 app_version 匹配的 map 文件。

    优先级（从高到低）：
      1. 同目录下 rosetta_map.*.json 中 _meta.app_version_range 命中的第一个（按文件名排序）
      2. default_map_path（默认文件，始终作为最终兜底）

    Args:
        default_map_path: 当前注册的默认 map 路径（如 apps/sample_web/rosetta_map.json）
        app_version:      声明的被测 App 版本字符串（如 "2.1.3"）；
                          空串 → 直接返回 default_map_path（零回归）

    Returns:
        应该加载的 map 文件绝对/相对路径
    """
    if not app_version or not app_version.strip():
        return default_map_path

    ver_tuple = _parse_version(app_version)
    if ver_tuple is None:
        logger.warning(
            "[rosetta_version] 版本号 %r 格式无法解析，回退默认 map: %s",
            app_version, os.path.basename(default_map_path),
        )
        return default_map_path

    dir_path = os.path.dirname(os.path.abspath(default_map_path))
    # 扫描 rosetta_map.*.json（不含 rosetta_map.json 本身）
    candidates = sorted(
        f for f in glob.glob(os.path.join(dir_path, "rosetta_map.*.json"))
        if os.path.isfile(f)
    )

    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8") as fh:
                meta = json.load(fh).get("_meta", {})
            range_str = meta.get("app_version_range", "").strip()
            if _version_matches(ver_tuple, range_str):
                logger.info(
                    "[rosetta_version] app=%s version=%s → 命中 %s（range=%r）",
                    _app_name_from_path(default_map_path),
                    app_version,
                    os.path.basename(candidate),
                    range_str,
                )
                return candidate
        except Exception as exc:
            logger.debug(
                "[rosetta_version] 读取候选 map 失败（跳过）: %s — %s",
                os.path.basename(candidate), exc,
            )

    logger.info(
        "[rosetta_version] app=%s version=%s 无匹配版本 map，使用默认 %s",
        _app_name_from_path(default_map_path),
        app_version,
        os.path.basename(default_map_path),
    )
    return default_map_path


def _app_name_from_path(map_path: str) -> str:
    """从 map 路径提取 app 名（供日志展示）。"""
    return os.path.basename(os.path.dirname(os.path.abspath(map_path))) or "unknown"
