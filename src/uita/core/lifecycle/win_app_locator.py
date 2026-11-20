"""win_app_locator.py — 通用 Windows 应用可执行文件安装路径解析器。

背景
====
Windows 应用安装包常把可执行文件放到带版本号的子目录（如
``C:\\Program Files\\<App>\\v1.2.3\\<App>.exe``），固定硬编码路径在该场景下
会返回过期/不存在的路径，直接 ``subprocess.Popen`` 必报 ``FileNotFoundError``。
本模块提供**通用**的多级解析链，供任意 app
复用，不含任何 app 专属字符串：

    env 缓存 → 桌面/开始菜单快捷方式(.lnk 反查 TargetPath)
      → 已知安装目录 + 版本号子目录通配扫描(取最大版本)
      → Windows 注册表 Uninstall 项(InstallLocation/DisplayIcon)
      → 指定根目录深度搜索(兜底)

各 app 只需在 ``apps/<name>/`` 下提供一层薄封装（传入自己的 exe 名称/候选
安装目录/候选快捷方式路径/注册表匹配关键字/env 缓存变量名），即可复用全部
算法，无需重复实现。参考：``apps/sample_web/sample_win_path.py``。

参数化设计：将算法与具体 App 名称/exe 字符串完全解耦，全部通过函数参数传入。

零外部依赖（R2）：仅 stdlib（``os``/``re``/``sys``/``pathlib``/条件
``winreg``）+ 可选 ``pywin32``（缺失时 ``_read_lnk_target_com`` 自动降级到
二进制兜底解析 ``.lnk``，不报错）。不 import ``engine/``、``apps/``、
``cli/``、``tools/`` 任何模块。非 win32 平台所有函数入口直接返回 ``None``
（no-op）。

计划文档：docs/plans/archive/01-engine-lifecycle-实施纪要.md
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def resolve_win_app_path(
    exe_name: str,
    *,
    install_roots: Sequence[str] = (),
    shortcut_paths: Sequence[str] = (),
    registry_keywords: Sequence[str] = (),
    env_cache_key: str = "",
    deep_search_root: str = "",
    deep_search_max_depth: int = 3,
    force: bool = False,
    persist_env_local: bool = False,
) -> Optional[str]:
    """通用 Windows 应用可执行文件路径解析（多级解析链，任意 app 可复用）。

    解析顺序（高 → 低）：env 缓存 → 快捷方式(.lnk 反查) → 安装目录+版本子目录
    扫描(取最大版本) → 注册表 Uninstall 项 → 指定根目录深度搜索(兜底)。

    非 win32 平台或全部解析失败均返回 ``None``（不抛异常，不做任何默认值
    兜底——兜底路径由调用方自行决定，如各 app 自己的硬编码默认值）。

    Args:
        exe_name: 目标可执行文件名（如 ``"YourApp.exe"``），末尾匹配不区分大小写。
        install_roots: 候选安装根目录列表（会额外扫描 ``<root>/v*/`` 版本子目录）。
        shortcut_paths: 候选 ``.lnk`` 快捷方式完整路径列表（反查 TargetPath）。
        registry_keywords: 注册表 Uninstall 项 DisplayName/子键名匹配关键字
            （不区分大小写，任一命中即认为是目标应用）。
        env_cache_key: 进程级 env 缓存变量名（非空时启用缓存）。缓存命中时
            **不会**无条件信任：先做一次低成本复核（见下方"缓存复核"），
            确认仍与权威来源一致才直接返回；文件不存在或复核不一致时自动
            失效并重新探测/纠正。
        deep_search_root: 深搜兜底根目录（通常传 ``%LOCALAPPDATA%``），为空
            则跳过深搜。
        deep_search_max_depth: 深搜最大目录层数（相对 ``deep_search_root``）。
        force: ``True`` 时跳过 env 缓存与复核，强制走完整解析链重新探测。
        persist_env_local: ``True`` 时缓存写入/纠正时额外写入项目根
            ``.env.local``（跨进程持久化，见
            ``core/lifecycle/env_local_store.py``），默认 ``False`` 只写当前
            进程 ``os.environ``（零外部依赖，行为不变）。该参数为
            ``core/lifecycle`` 内部同层依赖（R3 允许），不违反 R2（不引入
            ``engine/``/``apps/``/``cli/`` 依赖）。

    缓存复核（新旧版本共存场景，见需求背景）：
        Windows 应用升级常见"新旧版本目录同时存在于磁盘"（旧 exe 未被卸载
        程序清理），此时仅凭 ``os.path.exists(cached)`` 判断缓存有效性会长期
        误信旧版本路径。因此缓存命中时会用**低成本**权威来源（快捷方式 →
        安装目录扫描 → 注册表，均不含开销较大的深度搜索 ``os.walk``）重新核
        对一次：
          - 权威来源无信号（三者均未命中，如离线/未建快捷方式环境）→ 信任
            缓存，直接返回，不产生任何多余探测/写入（满足"不会重复探测"）。
          - 权威来源命中且与缓存一致 → 信任缓存，直接返回，不重复写入。
          - 权威来源命中且与缓存**不一致**（判定为旧版本残留）→ 丢弃缓存，
            采用权威来源解析出的新路径，并立即回写缓存（env/.env.local），
            确保后续调用读到的永远是最新正确路径。
        深度搜索仍只在"权威来源三者也全部落空"时作为最终兜底触发（该分支
        本身开销较大，不在每次缓存命中时都执行，兼顾正确性与性能）。

    Returns:
        解析成功的绝对路径字符串；全部失败或非 win32 平台返回 ``None``。
    """
    if sys.platform != "win32":
        return None

    if env_cache_key and not force:
        cached = os.environ.get(env_cache_key, "").strip()
        if cached:
            if not os.path.exists(cached):
                logger.debug("[win_app_locator] 缓存路径已失效（文件不存在），重新探测：%s", cached)
                os.environ.pop(env_cache_key, None)
            else:
                authoritative = (
                    _resolve_via_shortcut(shortcut_paths, exe_name)
                    or _resolve_via_install_dirs(install_roots, exe_name)
                    or _resolve_via_registry(registry_keywords, exe_name)
                )
                if authoritative is None:
                    # 无权威来源信号，信任现有缓存，避免无意义的重复探测
                    return cached
                if _same_path(authoritative, cached):
                    return cached
                # 权威来源指向另一个真实存在的路径 → 判定缓存为旧版本残留
                logger.warning(
                    "[win_app_locator] 缓存路径与权威来源（快捷方式/安装目录/注册表）不一致，"
                    "判定为旧版本残留，自动纠正：%s → %s",
                    cached, authoritative,
                )
                _write_cache(env_cache_key, authoritative, persist_env_local)
                return authoritative

    resolved = (
        _resolve_via_shortcut(shortcut_paths, exe_name)
        or _resolve_via_install_dirs(install_roots, exe_name)
        or _resolve_via_registry(registry_keywords, exe_name)
        or _resolve_via_deep_search(deep_search_root, exe_name, deep_search_max_depth)
    )

    if resolved and env_cache_key:
        _write_cache(env_cache_key, resolved, persist_env_local)

    return resolved


def _same_path(a: str, b: str) -> bool:
    """路径等价比较（大小写不敏感 + 规范化绝对路径，Windows 路径语义）。"""
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:  # noqa: BLE001
        return a == b


def _write_cache(env_cache_key: str, value: str, persist_env_local: bool) -> None:
    """写入解析结果到缓存：``persist_env_local=True`` 落盘 ``.env.local``
    （跨进程持久化），否则只写当前进程 ``os.environ``。"""
    if persist_env_local:
        from uita.core.lifecycle.env_local_store import upsert_env_local

        upsert_env_local(env_cache_key, value)
        logger.info(
            "[win_app_locator] 路径已确定 → %s（已持久化到 .env.local，key=%s）",
            value, env_cache_key,
        )
    else:
        os.environ[env_cache_key] = value
        logger.info(
            "[win_app_locator] 路径已确定 → %s（已缓存到 env %s）",
            value, env_cache_key,
        )


# ===================== 快捷方式（.lnk） =====================

def _resolve_via_shortcut(shortcut_paths: Sequence[str], exe_name: str) -> Optional[str]:
    """读候选 ``.lnk`` 的 TargetPath。优先用 pywin32 + COM；失败则二进制兜底。"""
    exe_lower = exe_name.lower()
    for lnk in shortcut_paths:
        if not lnk or not os.path.exists(lnk):
            continue
        target = _read_lnk_target_com(lnk) or _read_lnk_target_binary(lnk, exe_name)
        if target and target.lower().endswith(exe_lower) and os.path.exists(target):
            logger.info("[win_app_locator] 通过快捷方式定位：%s → %s", lnk, target)
            return target
    return None


def _read_lnk_target_com(lnk_path: str) -> Optional[str]:
    """用 pywin32 + WScript.Shell COM 读 .lnk 的 TargetPath。"""
    try:
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(lnk_path)
        target = getattr(sc, "TargetPath", "") or ""
        return target.strip() or None
    except Exception as e:  # noqa: BLE001  pywin32 缺失或 COM 异常
        logger.debug("[win_app_locator] COM 读 .lnk 失败：%s（%s）", lnk_path, e)
        return None


def _read_lnk_target_binary(lnk_path: str, exe_name: str) -> Optional[str]:
    """二进制兜底解析 .lnk（不依赖 pywin32）。

    LNK 文件格式参考 [MS-SHLLINK]。这里只做"足够用"的解析：搜索文件中所有
    ASCII / UTF-16LE 子串，取第一个末尾匹配 ``exe_name`` 的绝对路径。
    """
    try:
        with open(lnk_path, "rb") as f:
            data = f.read()
    except Exception:  # noqa: BLE001
        return None

    candidates: List[str] = []
    for m in re.finditer(rb"[A-Za-z]:\\[ -~]{3,260}?\.exe", data):
        try:
            candidates.append(m.group(0).decode("ascii", errors="ignore"))
        except Exception:  # noqa: BLE001
            pass
    try:
        text16 = data.decode("utf-16-le", errors="ignore")
    except Exception:  # noqa: BLE001
        text16 = ""
    for m in re.finditer(r"[A-Za-z]:\\[^\x00<>:\"|?*]{3,260}?\.exe", text16):
        candidates.append(m.group(0))

    exe_lower = exe_name.lower()
    for c in candidates:
        if c.lower().endswith(exe_lower) and os.path.exists(c):
            return c
    return None


# ===================== 安装目录 + 版本子目录 =====================

def _resolve_via_install_dirs(install_roots: Sequence[str], exe_name: str) -> Optional[str]:
    """扫已知安装根目录：先看 ``<root>\\<exe_name>``（旧版本/系统级安装），
    再看 ``<root>\\v*\\<exe_name>``（新版本带版本号子目录），取版本号最大者。"""
    versioned: List[Tuple[Tuple[int, ...], str]] = []
    for root in install_roots:
        if not root or not os.path.isdir(root):
            continue

        direct = Path(root) / exe_name
        if direct.exists():
            return str(direct)

        try:
            for sub in Path(root).iterdir():
                if not sub.is_dir():
                    continue
                exe = sub / exe_name
                if not exe.exists():
                    continue
                ver = _parse_version_tuple(sub.name)
                versioned.append((ver, str(exe)))
        except Exception as e:  # noqa: BLE001
            logger.debug("[win_app_locator] 扫目录失败 %s：%s", root, e)
            continue

    if versioned:
        versioned.sort(reverse=True)
        return versioned[0][1]
    return None


def _parse_version_tuple(name: str) -> Tuple[int, ...]:
    """把 ``v1.2.3`` / ``1.2-4001-3`` 等转成可比较的 int 元组，解析失败返回 ``(0,)``。"""
    nums = re.findall(r"\d+", name or "")
    if not nums:
        return (0,)
    try:
        return tuple(int(n) for n in nums)
    except Exception:  # noqa: BLE001
        return (0,)


# ===================== 注册表 =====================

def _resolve_via_registry(registry_keywords: Sequence[str], exe_name: str) -> Optional[str]:
    """从 Uninstall 注册表项的 InstallLocation / DisplayIcon 推断 .exe 路径。"""
    if sys.platform != "win32" or not registry_keywords:
        return None
    try:
        import winreg  # type: ignore
    except ImportError:
        return None

    keywords_lower = [k.lower() for k in registry_keywords if k]
    if not keywords_lower:
        return None
    exe_lower = exe_name.lower()

    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for root_key, key_path in keys:
        try:
            key = winreg.OpenKey(root_key, key_path, 0, winreg.KEY_READ)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    sk = winreg.OpenKey(key, sub)
                except OSError:
                    continue
                try:
                    try:
                        dn, _ = winreg.QueryValueEx(sk, "DisplayName")
                    except OSError:
                        dn = ""
                    dn_lower = (dn or "").lower()
                    sub_lower = sub.lower()
                    if not any(kw in dn_lower or kw in sub_lower for kw in keywords_lower):
                        continue

                    try:
                        loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                        if loc:
                            cand = os.path.join(loc.strip().rstrip("\\"), exe_name)
                            if os.path.exists(cand):
                                return cand
                            for sub_dir in Path(loc).iterdir() if Path(loc).is_dir() else []:
                                exe = sub_dir / exe_name
                                if exe.exists():
                                    return str(exe)
                    except OSError:
                        pass

                    try:
                        icon, _ = winreg.QueryValueEx(sk, "DisplayIcon")
                        if icon:
                            cand = icon.split(",")[0].strip().strip('"')
                            if cand.lower().endswith(exe_lower) and os.path.exists(cand):
                                return cand
                    except OSError:
                        pass
                finally:
                    winreg.CloseKey(sk)
        finally:
            winreg.CloseKey(key)
    return None


# ===================== 深度搜索兜底 =====================

def _resolve_via_deep_search(root: str, exe_name: str, max_depth: int) -> Optional[str]:
    """在 ``root`` 下深度 ≤``max_depth`` 层 ``os.walk`` 兜底搜索（大小写不敏感）。"""
    if not root or not os.path.isdir(root):
        return None
    exe_lower = exe_name.lower()
    sep_count_base = root.count(os.sep)
    for r, dirs, files in os.walk(root):
        for f in files:
            if f.lower() == exe_lower:
                return os.path.join(r, f)
        if r.count(os.sep) - sep_count_base > max_depth:
            dirs.clear()
    return None


__all__ = ["resolve_win_app_path"]
