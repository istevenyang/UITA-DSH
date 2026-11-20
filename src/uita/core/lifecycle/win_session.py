"""Windows 会话 / 窗口站诊断（只读）。

被测 GUI 应用的窗口与托盘图标创建在"调用进程所属会话的交互桌面"上。若 UITA 由
服务 / 计划任务 / 远程下发的 agent 拉起，落在 Session 0 或非 `WinSta0` 窗口站，则被测
应用进程树完整、CDP 可连，但用户在登录桌面上看不到窗口与托盘图标。

本模块只做只读探测并输出诊断日志，**不改变任何执行行为**；探测失败整体吞掉，
绝不成为新的启动失败点。非 win32 平台所有函数均安全返回 None。

判定口径：
  - Session 0 一律视为非交互（Vista+ 服务会话隔离）；
  - 窗口站名为 `WinSta0` 视为交互桌面；
  - **不**拿"当前会话 == console 会话"做依据：RDP 会话下的用户同样交互但不是
    console 会话，按 console 比较会误报。active_sessions 仅供人工对照。

设计依据见 docs/plans/20261020100000-windows-interactive-session-diagnose-relaunch.md。
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

_SKIP_ENV = "UITA_SKIP_WIN_SESSION_CHECK"
_INTERACTIVE_WINSTA = "WinSta0"


@dataclass(frozen=True)
class ActiveSession:
    """WTSActive 状态的会话。"""

    session_id: int
    station_name: str
    user: str


@dataclass(frozen=True)
class SessionInfo:
    session_id: Optional[int]
    window_station: Optional[str]
    active_sessions: tuple[ActiveSession, ...]
    # True 交互桌面 / False 非交互 / None 无法判定
    interactive: Optional[bool]


class _Win32Api:
    """Win32 只读查询薄封装。测试时整体替换 `_win_api()` 的返回值。"""

    def current_session_id(self) -> Optional[int]:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        sid = wintypes.DWORD(0)
        if not kernel32.ProcessIdToSessionId(
            kernel32.GetCurrentProcessId(), ctypes.byref(sid)
        ):
            return None
        return int(sid.value)

    def window_station_name(self) -> Optional[str]:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetProcessWindowStation.restype = wintypes.HANDLE
        user32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        hws = user32.GetProcessWindowStation()
        if not hws:
            return None
        uoi_name = 2
        needed = wintypes.DWORD(0)
        buf = ctypes.create_unicode_buffer(256)
        if not user32.GetUserObjectInformationW(
            hws, uoi_name, buf, ctypes.sizeof(buf), ctypes.byref(needed)
        ):
            return None
        return buf.value or None

    def active_sessions(self) -> list[ActiveSession]:
        import ctypes
        from ctypes import wintypes

        class _WtsSessionInfo(ctypes.Structure):
            _fields_ = [
                ("SessionId", wintypes.DWORD),
                ("pWinStationName", wintypes.LPWSTR),
                ("State", ctypes.c_int),
            ]

        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        wts.WTSEnumerateSessionsW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_WtsSessionInfo)),
            ctypes.POINTER(wintypes.DWORD),
        ]
        wts.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
        ]
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]

        pinfo = ctypes.POINTER(_WtsSessionInfo)()
        count = wintypes.DWORD(0)
        if not wts.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(pinfo), ctypes.byref(count)):
            return []
        result: list[ActiveSession] = []
        try:
            wts_active = 0
            wts_user_name = 5
            for i in range(count.value):
                item = pinfo[i]
                if item.State != wts_active:
                    continue
                user = ""
                pbuf = ctypes.c_void_p()
                blen = wintypes.DWORD(0)
                if wts.WTSQuerySessionInformationW(
                    None, item.SessionId, wts_user_name,
                    ctypes.byref(pbuf), ctypes.byref(blen),
                ):
                    try:
                        user = ctypes.wstring_at(pbuf.value) if pbuf.value else ""
                    finally:
                        wts.WTSFreeMemory(pbuf)
                result.append(ActiveSession(
                    session_id=int(item.SessionId),
                    station_name=item.pWinStationName or "",
                    user=user,
                ))
        finally:
            wts.WTSFreeMemory(ctypes.cast(pinfo, ctypes.c_void_p))
        return result


def _win_api() -> _Win32Api:
    return _Win32Api()


def _judge_interactive(session_id: Optional[int], window_station: Optional[str]) -> Optional[bool]:
    if session_id == 0:
        return False
    if window_station:
        return window_station == _INTERACTIVE_WINSTA
    return None


def get_session_info() -> Optional[SessionInfo]:
    """采集当前进程会话信息；非 win32 或完全无法采集返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        api = _win_api()
    except Exception:
        return None

    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    session_id = _safe(api.current_session_id, None)
    window_station = _safe(api.window_station_name, None)
    active = tuple(_safe(api.active_sessions, []) or [])
    if session_id is None and window_station is None and not active:
        return None
    return SessionInfo(
        session_id=session_id,
        window_station=window_station,
        active_sessions=active,
        interactive=_judge_interactive(session_id, window_station),
    )


def _format_active(active: tuple[ActiveSession, ...]) -> str:
    if not active:
        return "-"
    return ",".join(f"{a.session_id}:{a.station_name or '?'}:{a.user or '?'}" for a in active)


def log_session_diagnostics(log: logging.Logger) -> Optional[SessionInfo]:
    """输出会话诊断。非交互时追加 WARNING。任何异常都吞掉，返回 None。"""
    if sys.platform != "win32":
        return None
    if os.environ.get(_SKIP_ENV, "") == "1":
        return None
    try:
        info = get_session_info()
        if info is None:
            return None
        log.info(
            "[win_session] session_id=%s window_station=%s interactive=%s active_sessions=%s",
            info.session_id, info.window_station, info.interactive,
            _format_active(info.active_sessions),
        )
        if info.interactive is False:
            log.warning(
                "[win_session] 当前进程不在交互桌面（session_id=%s window_station=%s）："
                "被测 GUI 应用的窗口与托盘图标将创建在不可见桌面，登录用户看不到。"
                "请让 报告平台 agent / UITA 在交互登录会话中运行，"
                "详见 docs/plans/20261020100000-windows-interactive-session-diagnose-relaunch.md。"
                "可设 %s=1 关闭本诊断。活动会话: %s",
                info.session_id, info.window_station, _SKIP_ENV,
                _format_active(info.active_sessions),
            )
        return info
    except Exception:
        return None
