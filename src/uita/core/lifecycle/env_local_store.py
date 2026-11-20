"""env_local_store.py — 读写项目根 .env.local 的 KEY=VALUE 行（跨进程持久化）。

背景：`--mode install` 是独立 CLI 调用，写入当前进程 `os.environ` 只在本进程
生效；后续 `--mode runner`/`--mode batch` 是新进程，需要从 `.env.local` 读取
（`settings.py` 启动时加载进 `os.environ`）才能跨进程生效。`.env.local` 已在
`.gitignore` 中（不入库），本模块只是新增一类写入方，不改变 `settings.py`
现有加载逻辑（无差别 KEY=VALUE 解析）。

见 docs/plans/archive/07-db-data-实施纪要.md §3.5。

零外部依赖（R2）：仅 stdlib。
"""
from __future__ import annotations

import os
from typing import Optional

from uita.config import ROOT_DIR

_ENV_FILE = os.path.join(ROOT_DIR, ".env.local")
_MANAGED_SECTION_HEADER = "# Installer-managed path cache (--mode install 自动写入，请勿手工编辑此分区下方内容)"
_FILE_ATTRIBUTE_HIDDEN = 0x2  # Windows FILE_ATTRIBUTE_HIDDEN，仅 nt 平台使用


def upsert_env_local(key: str, value: str, env_file: Optional[str] = None) -> None:
    """更新/新增 .env.local 中的 KEY=VALUE 行。

    - 若 key 已存在（任意位置的 `KEY=...` 行，忽略前后空白），原地替换该行的值，
      其他行保持不变。
    - 若 key 不存在，追加到文件末尾的 "Installer-managed path cache" 分区下
      （分区标题只写一次，重复调用不会重复插入分区头）。
    - 同时写入当前进程 `os.environ[key]`（即时生效，不等待下次进程重启）。

    Args:
        key: 环境变量名（如 "UITA_SAMPLE_WIN_PATH"）。
        value: 环境变量值。
        env_file: 覆盖默认的 .env.local 路径（单测用，默认 None 时用项目根 .env.local）。
    """
    target = env_file or _ENV_FILE
    key = key.strip()
    if not key:
        raise ValueError("upsert_env_local: key 不能为空")

    lines: list[str] = []
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as fh:
            lines = fh.readlines()

    new_line = f"{key}={value}\n"
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        existing_key, _, _ = stripped.partition("=")
        if existing_key.strip() == key:
            lines[i] = new_line
            replaced = True
            break

    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        has_managed_section = any(_MANAGED_SECTION_HEADER in line for line in lines)
        if not has_managed_section:
            if lines:
                lines.append("\n")
            lines.append(_MANAGED_SECTION_HEADER + "\n")
        lines.append(new_line)

    os.makedirs(os.path.dirname(target), exist_ok=True)

    # 用"临时文件 + os.replace() 原子替换"而非直接 open(target, "w")（截断重建）。
    #
    # 根因（真机验证发现，2026-07-28）：Windows 下 open(path, "w") 底层调用
    # CreateFile(..., CREATE_ALWAYS)，若目标文件已存在且带 Hidden/System 属性
    # （.env.local 这类点前缀文件常被编辑器/工具自动打上 Hidden 属性），CRT 默认
    # 不会带上原文件的 dwFlagsAndAttributes，导致 CREATE_ALWAYS 截断重建被系统
    # 拒绝，抛 `PermissionError: [Errno 13]`——但 append 模式（"a"，不走
    # CREATE_ALWAYS）不受影响，这是本问题"间歇性/看起来像权限配置错误"的表现，
    # 实际是"w"截断写入模式与 Hidden 属性文件的固有冲突，与 ACL/账户权限无关。
    #
    # os.replace()（Windows 下走 MoveFileEx + MOVEFILE_REPLACE_EXISTING）对
    # Hidden/System 属性文件没有这个限制，且天然原子（不会因写入中途异常/进程被
    # kill 导致目标文件半截损坏），是更稳妥的通用修复，不需要用户手工调整文件
    # 属性、不依赖 ACL 状态。
    # Windows 下 os.replace() 用新文件整体替换旧文件，不会继承旧文件的
    # Hidden/System 等属性（验证：替换后 attrib 显示只剩 Archive，Hidden 丢失）
    # ——若原文件带 Hidden 属性，先记下来，替换后重新打上，避免"改一次配置，
    # 隐藏的点文件变成可见文件"这种意外副作用。仅 Windows 需要（Hidden 是
    # Windows 文件属性概念，`os.stat` 在 POSIX 上没有 st_file_attributes）。
    was_hidden = False
    if os.name == "nt" and os.path.isfile(target):
        try:
            was_hidden = bool(os.stat(target).st_file_attributes & _FILE_ATTRIBUTE_HIDDEN)
        except (AttributeError, OSError):
            was_hidden = False

    tmp_target = f"{target}.tmp{os.getpid()}"
    try:
        with open(tmp_target, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        os.replace(tmp_target, target)
    except Exception:
        try:
            if os.path.isfile(tmp_target):
                os.remove(tmp_target)
        except OSError:
            pass
        raise

    if was_hidden:
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(target, _FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            pass  # 尽力而为：恢复 Hidden 属性失败不应阻断写入本身已成功的事实

    os.environ[key] = value


def remove_env_local(key: str, env_file: Optional[str] = None) -> None:
    """从 .env.local 删除指定 key（不存在时静默，不报错）。

    用于卸载流程清理缓存的安装路径。同时清除当前进程 ``os.environ`` 中的 key。

    Args:
        key: 环境变量名（如 "UITA_Demo_GALLERY_PATH"）。
        env_file: 覆盖默认的 .env.local 路径（单测用）。
    """
    target = env_file or _ENV_FILE
    key = key.strip()
    if not key:
        return

    if not os.path.isfile(target):
        os.environ.pop(key, None)
        return

    with open(target, encoding="utf-8") as fh:
        lines = fh.readlines()

    filtered = [
        line for line in lines
        if not _is_key_line(line, key)
    ]

    if len(filtered) == len(lines):
        os.environ.pop(key, None)
        return

    tmp_target = f"{target}.tmp{os.getpid()}"
    try:
        with open(tmp_target, "w", encoding="utf-8") as fh:
            fh.writelines(filtered)
        os.replace(tmp_target, target)
    except Exception:
        try:
            if os.path.isfile(tmp_target):
                os.remove(tmp_target)
        except OSError:
            pass
        raise

    os.environ.pop(key, None)


def _is_key_line(line: str, key: str) -> bool:
    """判断一行是否为指定 key 的 KEY=VALUE 行。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return False
    existing_key, _, _ = stripped.partition("=")
    return existing_key.strip() == key


__all__ = ["upsert_env_local", "remove_env_local"]
