"""win_elevation.py — Windows 提权启动原语（WinError 740 兜底）。

背景：部分安装器/卸载器 exe 的清单声明 ``requireAdministrator``，非提权会话里
``subprocess.Popen`` 直接被 OS 拒绝，抛
``OSError(winerror=740, "请求的操作需要提升")``（``ERROR_ELEVATION_REQUIRED``）——
此时 ``CreateProcess`` 根本没成功，没有可等待的进程对象。

本模块把「用 ``ShellExecuteExW("runas")`` 提权重启 + 等它退出」收敛成一个小原语，
供 ``app_installer.py`` / ``app_uninstaller.py`` 复用，避免各处裸调 ``Popen``：

  - ``is_elevation_required(exc)``：判定异常是否为 740；
  - ``launch_elevated(exe_path, args)``：提权启动，成功返回进程句柄
    （``SEE_MASK_NOCLOSEPROCESS`` 要求调用方负责释放），失败返回 ``None``；
  - ``wait_for_process(handle, timeout_s)``：等提权进程退出（best-effort）；
  - ``close_process_handle(handle)``：释放句柄（best-effort）。

设计取舍（对齐 ``apps/sample_web/app_prepare_helper.py`` 的真机结论，
但只取「启动 + 等待」这一半）：该先例真机复现出「同一进程内连续发起两次
``"runas"`` 请求不稳定」，故本模块**只发起一次**提权请求，拿到句柄后仅用于
等待退出与释放，不做 ``TerminateProcess`` 强杀（安装/卸载场景不需要）。

会话约束（真机已知，非本模块可绕过）：``"runas"`` 需要交互桌面，或当前会话
已提权。无头 Session 0 / UAC 被取消时 ``ShellExecuteExW`` 立即失败 →
``launch_elevated`` 返回 ``None``，由调用方抛带处置指引的错误。

仅依赖标准库（ctypes / subprocess），零 app 专属字符串（R2、R13）。
见 docs/plans/done/archive_202610.md。
"""
from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# ERROR_ELEVATION_REQUIRED：exe 清单要求管理员权限，非提权 CreateProcess 被拒。
ERROR_ELEVATION_REQUIRED = 740

# ShellExecuteExW 标志位：SEE_MASK_NOCLOSEPROCESS（要求返回 hProcess，调用方负责 CloseHandle）。
SEE_MASK_NOCLOSEPROCESS = 0x00000040

# WaitForSingleObject 返回值：WAIT_OBJECT_0（对象已置信号，即进程已退出）。
_WAIT_OBJECT_0 = 0x00000000


def is_elevation_required(exc: BaseException) -> bool:
    """异常是否为 ``WinError 740``（``ERROR_ELEVATION_REQUIRED``）。

    非 ``OSError``（没有 ``winerror`` 属性）一律返回 ``False``，不猜测。
    """
    return getattr(exc, "winerror", None) == ERROR_ELEVATION_REQUIRED


def launch_elevated(exe_path: str, args: Sequence[str] = ()) -> Optional[int]:
    """用 ``ShellExecuteExW("runas")`` 提权启动 ``exe_path``。

    成功返回该进程句柄（``hProcess``，须由调用方 ``close_process_handle`` 释放）；
    以下情况均返回 ``None``（best-effort，不上抛）：

      - 非 Windows 平台（no-op，便于跨平台调用点无需自行判平台）；
      - ``exe_path`` 为空；
      - UAC 被用户取消 / 当前会话为无交互桌面（``ShellExecuteExW`` 失败）；
      - 系统调用抛异常。
    """
    if sys.platform != "win32" or not exe_path:
        return None

    params = subprocess.list2cmdline([str(a) for a in args]) if args else None
    try:
        return _shell_execute_exw_runas(exe_path, params)
    except Exception as exc:  # noqa: BLE001  best-effort
        logger.warning("[elevation] 提权启动 %s 异常（忽略）: %s", exe_path, exc)
        return None


def _shell_execute_exw_runas(exe_path: str, params: Optional[str]) -> Optional[int]:
    """真实 Win32 调用（单测 mock 的 seam）：``ShellExecuteExW`` + ``"runas"``。

    返回 ``hProcess`` 句柄；调用失败或未返回有效句柄时返回 ``None``。
    ``params`` 为已由 ``subprocess.list2cmdline`` 组装好的参数字符串（可空）。
    """
    from ctypes import wintypes

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HANDLE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HANDLE),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = exe_path
    info.lpParameters = params
    info.lpDirectory = None
    info.nShow = 1
    info.hInstApp = None
    info.lpIDList = None
    info.lpClass = None
    info.hkeyClass = None
    info.dwHotKey = 0
    info.hIconOrMonitor = None
    info.hProcess = None

    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        logger.warning(
            "[elevation] ShellExecuteExW('runas') 失败或被取消: %s", exe_path,
        )
        return None
    if not info.hProcess:
        logger.warning("[elevation] ShellExecuteExW('runas') 未返回有效进程句柄: %s", exe_path)
        return None
    return int(info.hProcess)


def wait_for_process(handle: int, timeout_s: float) -> bool:
    """等待提权进程退出。

    在 ``timeout_s`` 内退出返回 ``True``；超时、句柄无效、非 win32 或系统调用
    异常返回 ``False``（best-effort，不上抛——调用方已持有「提权启动成功」的事实，
    等待失败不应升级为安装失败，后续路径轮询会给出真实结论）。
    """
    if not handle or sys.platform != "win32":
        return False
    try:
        timeout_ms = max(0, int(timeout_s * 1000))
        rc = ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(int(handle)), timeout_ms)
        return int(rc) == _WAIT_OBJECT_0
    except Exception as exc:  # noqa: BLE001  best-effort
        logger.warning("[elevation] WaitForSingleObject 等待提权进程异常（忽略）: %s", exc)
        return False


def close_process_handle(handle: int) -> None:
    """释放 ``launch_elevated`` 持有的进程句柄（``SEE_MASK_NOCLOSEPROCESS`` 要求）。"""
    if not handle or sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(int(handle)))
    except Exception as exc:  # noqa: BLE001  best-effort
        logger.warning("[elevation] CloseHandle 释放进程句柄异常（忽略）: %s", exc)


__all__ = [
    "ERROR_ELEVATION_REQUIRED",
    "SEE_MASK_NOCLOSEPROCESS",
    "is_elevation_required",
    "launch_elevated",
    "wait_for_process",
    "close_process_handle",
]
