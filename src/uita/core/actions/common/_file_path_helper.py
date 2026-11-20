"""choose_file[] 路径解析辅助（P1 新增）。

提供 5 级路径解析优先级，抽成独立函数供 core/actions/common/action_choose_file.py 调用，且统一使用
``settings.ROOT_DIR``（符合 R15：项目根路径统一用 ``from uita.config import
ROOT_DIR``，严禁用 ``__file__``/``os.getcwd()`` 硬编码）。

计划文档：docs/plans/archive/02-dsl-actions-实施纪要.md §3.6
"""
from __future__ import annotations

import os

from uita.config import ROOT_DIR


def resolve_file_path(raw: str) -> str:
    """解析文件/文件夹路径，返回校验存在的绝对路径。

    优先级（高 → 低）：
      1. ``~`` / ``~user`` 家目录展开 + ``$VAR``/``${VAR}`` 环境变量展开
      2. 绝对路径 → 校验存在后直接返回
      3. 相对路径 → 优先相对**项目根**（``settings.ROOT_DIR``），存在则返回
      4. 兜底相对**当前工作目录**（``os.getcwd()``），存在则返回

    全部失败时抛 ``FileNotFoundError``，错误信息列出所有尝试过的路径。
    """
    expanded = os.path.expandvars(os.path.expanduser(raw))

    if os.path.isabs(expanded):
        if not os.path.exists(expanded):
            raise FileNotFoundError(
                f"choose_file: 路径不存在（绝对路径）: {expanded}（原始输入: {raw}）"
            )
        return expanded

    rel_to_root = os.path.normpath(os.path.join(ROOT_DIR, expanded))
    if os.path.exists(rel_to_root):
        return rel_to_root

    rel_to_cwd = os.path.normpath(os.path.join(os.getcwd(), expanded))
    if os.path.exists(rel_to_cwd):
        return rel_to_cwd

    raise FileNotFoundError(
        f"choose_file: 路径不存在，已尝试以下位置：\n"
        f"  - 原始输入  : {raw}\n"
        f"  - 展开后    : {expanded}\n"
        f"  - 相对项目根: {rel_to_root}\n"
        f"  - 相对 CWD  : {rel_to_cwd}\n"
        f"提示：支持 ~/xxx 家目录、$VAR 环境变量、绝对路径、相对项目根、相对 CWD"
    )


def resolve_relative_path(raw: str, must_exist: bool = True) -> str:
    """解析文件/文件夹路径，固定"相对项目根"语义（供 file_op[]/open_path[] 使用）。

    与 ``resolve_file_path()``（choose_file[] 专用，级联尝试项目根→CWD 两处）
    不同，本函数语义更明确、行为可预测，不做级联猜测：

      1. ``~`` / ``~user`` 家目录展开 + ``$VAR``/``${VAR}`` 环境变量展开
      2. 绝对路径 → 直接使用（``must_exist=True`` 时额外校验存在）
      3. 相对路径 → 固定拼接项目根（``settings.ROOT_DIR``），**不做 CWD fallback**

    Args:
        raw: 原始输入路径（相对或绝对）。
        must_exist: ``True``（默认）时校验路径必须存在，否则抛
            ``FileNotFoundError``；``False`` 时仅做路径拼接/展开，不校验存在性
            （用于 ``dst``/``create``/``unzip`` 目标等允许尚不存在的场景）。

    Returns:
        解析后的绝对路径字符串（``must_exist=False`` 时可能是不存在的路径）。
    """
    expanded = os.path.expandvars(os.path.expanduser(raw))
    resolved = expanded if os.path.isabs(expanded) else os.path.normpath(
        os.path.join(ROOT_DIR, expanded)
    )

    if must_exist and not os.path.exists(resolved):
        raise FileNotFoundError(
            f"路径不存在: {resolved}（原始输入: {raw}，固定相对项目根拼接，"
            f"不做 CWD fallback）"
        )
    return resolved
