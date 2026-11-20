"""app_uninstaller.py — 跨平台 App 卸载编排（--mode install --uninstall 的卸载部分）。

与 app_installer.py 对称：安装是下载→覆盖安装，卸载是杀进程→删文件→清缓存。
所有 app 差异化参数来自 InstallConfig（apps/<name>/register.py 注册），
本文件不含任何 app 专属字符串（R2 零外部依赖）。

macOS：pkill → shutil.rmtree(.app) → 清 .env.local → 清 Electron 用户数据(best-effort)
Windows：kill → 读 Uninstall 注册表 → 执行卸载器(静默) → 轮询确认 exe 已删 → 兜底删目录 → 清 .env.local

Windows 卸载器若要求管理员权限（清单声明 requireAdministrator），非提权会话
``Popen`` 会抛 ``OSError(winerror=740)``；此时改用 ``win_elevation`` 提权重启
一次（可能弹 UAC 确认），失败则抛带处置指引的 UninstallError（与安装器对称，
见 docs/plans/done/archive_202610.md）。

见 docs/plans/archive/09-apps-business-实施纪要.md。
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional
from urllib.parse import urlparse

from uita.core.lifecycle.env_local_store import remove_env_local
from uita.core.lifecycle.win_elevation import (
    close_process_handle,
    is_elevation_required,
    launch_elevated,
    wait_for_process,
)
from uita.core.platform.install_registry import InstallConfig, InstallRegistry

logger = logging.getLogger(__name__)

_WIN_POLL_TIMEOUT_S = 120
_WIN_POLL_INTERVAL_S = 2


class UninstallError(Exception):
    """卸载流程失败（main.py 转换为 StartupError，退出码 1）。"""


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def uninstall_app(app_name: str) -> None:
    """卸载 app_name 对应的应用。

    失败抛 UninstallError。仅支持 macOS(darwin) / Windows(win32)。
    """
    if sys.platform == "darwin":
        platform_name = "macOS"
    elif sys.platform == "win32":
        platform_name = "Windows"
    else:
        raise UninstallError(f"卸载不支持当前平台: {sys.platform}（仅 macOS/Windows）")

    try:
        cfg = InstallRegistry.get(app_name, platform_name)
    except Exception as exc:
        raise UninstallError(str(exc)) from exc

    if platform_name == "macOS":
        _uninstall_mac(cfg)
    else:
        _uninstall_windows(cfg)


# ═══════════════════════════════════════════════════════════════
# macOS 卸载
# ═══════════════════════════════════════════════════════════════

def _uninstall_mac(cfg: InstallConfig) -> None:
    """macOS 卸载：杀进程 → 删 .app → 清 .env.local → 清 Electron 数据。"""
    # 1. 杀进程
    if cfg.mac_process_name:
        subprocess.run(
            ["pkill", "-x", cfg.mac_process_name],
            capture_output=True, text=True,
        )
        time.sleep(1)
        logger.info("[uninstall] 已尝试终止进程: %s", cfg.mac_process_name)

    # 2. 删 .app
    if cfg.mac_app_dst_path and os.path.exists(cfg.mac_app_dst_path):
        try:
            shutil.rmtree(cfg.mac_app_dst_path)
            logger.info("[uninstall] 已删除: %s", cfg.mac_app_dst_path)
        except Exception as exc:
            raise UninstallError(f"删除 .app 失败: {cfg.mac_app_dst_path}: {exc}") from exc
    else:
        logger.warning("[uninstall] .app 不存在，跳过删除: %s", cfg.mac_app_dst_path)

    # 3. 清 .env.local 缓存
    _env_cache_key = _resolve_env_cache_key(cfg, "macOS")
    if _env_cache_key:
        remove_env_local(_env_cache_key)
        logger.info("[uninstall] 已清除 .env.local 缓存: %s", _env_cache_key)

    # 4. 清 Electron 用户数据（best-effort，不阻断）
    if cfg.mac_process_name:
        _cleanup_mac_electron_data(cfg.mac_process_name)


def _cleanup_mac_electron_data(app_process_name: str) -> None:
    """best-effort 清理 macOS Electron 用户数据目录。

    失败仅 warning，不阻断卸载流程。
    """
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Library", "Application Support", app_process_name),
        os.path.join(home, "Library", "Application Support",
                     _app_name_to_bundle_id_hint(app_process_name)),
        os.path.join(home, "Library", "Caches", app_process_name),
        os.path.join(home, "Library", "Preferences",
                     f"com.example.{app_process_name.lower()}.plist"),
    ]
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            logger.debug("[uninstall] 已清理 Electron 数据: %s", path)
        except Exception as exc:  # noqa: BLE001  best-effort
            logger.warning("[uninstall] 清理 Electron 数据失败（忽略）: %s: %s", path, exc)


def _app_name_to_bundle_id_hint(app_name: str) -> str:
    """从 app_name 推测可能的 bundle_id 形式目录名（如 DemoApp → demoapp）。

    Electron 用户数据目录名可能是 app_name 原样，也可能是全小写。
    这里返回小写形式作为候选之一。
    """
    return app_name.lower() if app_name else ""


# ═══════════════════════════════════════════════════════════════
# Windows 卸载
# ═══════════════════════════════════════════════════════════════

def _uninstall_windows(cfg: InstallConfig) -> None:
    """Windows 卸载：杀进程 → 读注册表找卸载器 → 执行静默卸载 → 兜底删目录 → 清缓存。"""
    # 1. 杀进程
    if cfg.win_process_name:
        _kill_windows_process(cfg.win_process_name)

    # 2. 读 Uninstall 注册表，找 UninstallString
    uninstall_cmd = _find_uninstall_string(cfg.win_registry_keywords)

    # 3. 执行卸载器
    if uninstall_cmd:
        logger.info("[uninstall] 执行卸载器: %s", uninstall_cmd)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(uninstall_cmd + ["/S"], creationflags=creationflags)
            proc.wait(timeout=_WIN_POLL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.warning("[uninstall] 卸载器超时未退出，继续检查是否已删除")
        except OSError as exc:
            # WinError 740：卸载器清单要求管理员权限，非提权 CreateProcess 被 OS
            # 拒绝 → 改用 ShellExecuteExW("runas") 提权重启一次（与安装器对称）。
            if not is_elevation_required(exc):
                raise UninstallError(f"执行卸载器失败: {exc}") from exc
            logger.warning(
                "[uninstall] 卸载器要求管理员权限（WinError 740），改用提权方式执行"
                "（可能弹出 UAC 确认）: %s", uninstall_cmd,
            )
            elevated_handle = launch_elevated(uninstall_cmd[0], uninstall_cmd[1:] + ["/S"])
            if elevated_handle is None:
                raise UninstallError(
                    "执行卸载器失败: 卸载器要求管理员权限（WinError 740），自动提权失败"
                    "（UAC 被取消，或当前为无交互桌面会话）。请以管理员身份运行本进程后重试。"
                )
            try:
                wait_for_process(elevated_handle, _WIN_POLL_TIMEOUT_S)
            finally:
                close_process_handle(elevated_handle)
        except Exception as exc:
            raise UninstallError(f"执行卸载器失败: {exc}") from exc

        # 轮询确认 exe 已删除
        _poll_exe_removed(cfg)
    else:
        logger.warning("[uninstall] 注册表未找到卸载器，兜底直接删安装目录")

    # 4. 兜底：删安装目录残留
    for root in cfg.win_install_roots:
        if root and os.path.exists(root):
            try:
                shutil.rmtree(root, ignore_errors=True)
                logger.info("[uninstall] 已删除残留目录: %s", root)
            except Exception as exc:  # noqa: BLE001  best-effort
                logger.warning("[uninstall] 删除残留目录失败（忽略）: %s: %s", root, exc)

    # 5. 清 .env.local 缓存
    if cfg.win_env_cache_key:
        remove_env_local(cfg.win_env_cache_key)
        logger.info("[uninstall] 已清除 .env.local 缓存: %s", cfg.win_env_cache_key)


def _kill_windows_process(process_name: str) -> None:
    """终止 Windows 进程（复用 app_installer.py 的同名函数逻辑）。"""
    try:
        import psutil
    except ImportError:
        logger.debug("[uninstall] psutil 不可用，跳过关闭运行中实例")
        return
    target = process_name.lower()
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name == target or name == f"{target}.exe":
                proc.kill()
                logger.info("[uninstall] 已终止进程: %s (PID=%s)", name, proc.pid)
        except Exception:  # noqa: BLE001
            continue
    time.sleep(1)


def _find_uninstall_string(registry_keywords: List[str]) -> Optional[List[str]]:
    """从 Windows Uninstall 注册表查找 UninstallString，返回拆分后的命令列表。

    复用 win_app_locator.py::_resolve_via_registry 的注册表遍历逻辑，
    改为读 UninstallString 字段而非 InstallLocation/DisplayIcon。

    Args:
        registry_keywords: DisplayName 匹配关键字列表。

    Returns:
        命令列表（如 ["C:\\Program Files\\DemoGallery\\uninstall.exe"]），未找到返回 None。
    """
    if sys.platform != "win32" or not registry_keywords:
        return None
    try:
        import winreg  # type: ignore
    except ImportError:
        return None

    keywords_lower = [k.lower() for k in registry_keywords if k]
    if not keywords_lower:
        return None

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
                        uninstall_str, _ = winreg.QueryValueEx(sk, "UninstallString")
                        if uninstall_str:
                            return _parse_uninstall_string(uninstall_str)
                    except OSError:
                        pass
                finally:
                    winreg.CloseKey(sk)
        finally:
            winreg.CloseKey(key)
    return None


def _parse_uninstall_string(uninstall_str: str) -> List[str]:
    """拆分 UninstallString 为命令列表。

    NSIS 卸载器通常格式为：
      "C:\\Program Files\\DemoGallery\\uninstall.exe"
    或带引号：
      "C:\\Program Files\\DemoGallery\\uninstall.exe" /S
    """
    import shlex
    parts = shlex.split(uninstall_str)
    # 过滤掉已有的 /S 等参数（我们会自己加 /S）
    return [p for p in parts if p and not p.startswith("/")]


def _poll_exe_removed(cfg: InstallConfig) -> None:
    """轮询确认 exe 已被卸载器删除。"""
    deadline = time.monotonic() + _WIN_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        # 检查安装目录是否还存在
        exe_found = False
        for root in cfg.win_install_roots:
            if root and os.path.exists(root):
                # 在安装目录下搜索 exe
                exe_pattern = os.path.join(root, "**", cfg.win_exe_name)
                if glob.glob(exe_pattern, recursive=True):
                    exe_found = True
                    break
        if not exe_found:
            return
        time.sleep(_WIN_POLL_INTERVAL_S)
    logger.warning(
        "[uninstall] %ss 内 exe 仍存在，卸载器可能未完全清理",
        _WIN_POLL_TIMEOUT_S,
    )


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def _resolve_env_cache_key(cfg: InstallConfig, platform_name: str) -> str:
    """从 InstallConfig 获取 .env.local 缓存 key。

    macOS: 从 mac_app_dst_path 推导 env 变量名（如 UITA_Demo_GALLERY_PATH）。
           由于 macOS InstallConfig 不含 win_env_cache_key，需要按 app_name 推导。
    Windows: 直接用 cfg.win_env_cache_key。
    """
    if platform_name == "Windows" and cfg.win_env_cache_key:
        return cfg.win_env_cache_key
    # macOS：按约定 UITA_<APP_NAME_UPPER>_PATH
    return f"UITA_{cfg.app_name.upper()}_PATH"


__all__ = ["uninstall_app", "UninstallError"]
