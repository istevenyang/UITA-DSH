"""
AppProcess — Electron 应用进程 + CDP 端口管理。

职责：
  - 检测应用是否已在运行（按 CDP 端口精确匹配，find()/kill_by_port()）
  - 检测应用是否已有实例在运行（不限定 CDP 端口，is_running()）——用于识别
    "进程存在但未开调试端口"的遗留实例（Electron 单实例锁场景）
  - 未运行时跨平台启动（macOS / Windows / Linux），启动前若检测到遗留实例
    自动强制关闭（_start() 内部调用 kill_all()，macOS 用 pkill -x）
  - 轮询等待 CDP HTTP 接口就绪
  - 按端口精确杀进程（kill_by_port，不影响其他实例）/ 强制杀全部实例
    （kill_all，是 kill_by_port 的超集）

通用 Electron 应用进程与 CDP 调试端口生命周期管理器。

2026-07-03 补充（is_running/kill_all + _start() 自动强杀遗留实例）：修复
runner/batch 模式下"目标 App 已在运行但未带调试端口启动"导致
CDP:<port> 30s 超时的问题——根因是 Electron 单实例锁：新实例携带
`--remote-debugging-port` 参数启动时，若已有旧实例在运行，系统会把参数
转发给旧实例后让新实例自行退出，旧实例本身并不会因此打开调试端口，
`_wait_cdp()` 于是永远等不到端口就绪。修复后 `_start()` 启动前先探测
`is_running()`，命中则先 `kill_all()` 强制关闭（必要时 macOS 用 pkill -x），
再启动新实例，从而保证调试端口参数一定生效。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False
    logger.warning("psutil 未安装，进程探测功能不可用（pip install psutil）")


class AppNotRunningError(RuntimeError):
    """应用未运行且无法启动（路径不存在 / 权限不足等）。"""


class CDPTimeoutError(TimeoutError):
    """等待 CDP 就绪超时。"""


class AppProcess:
    """Electron 应用进程 + CDP 端口管理。

    用法::

        proc = AppProcess(app_path="/Applications/YourApp.app/Contents/MacOS/YourApp", cdp_port=9222)
        await proc.ensure_cdp()   # 确保 CDP 可连接，必要时自动启动
    """

    def __init__(
        self,
        app_path: str = "",
        cdp_port: int = 9222,
        app_name: str = "",
        extra_args: Optional[list[str]] = None,
    ) -> None:
        self.app_path = app_path
        self.cdp_port = cdp_port
        self.app_name = app_name or self._detect_app_name(app_path)
        self.extra_args = extra_args or []
        self.pid: Optional[int] = None
        self.launched_by_us = False

    # ── 公开方法 ────────────────────────────────────────────────

    async def ensure_cdp(self, timeout: float = 30.0) -> int:
        """确保 CDP 就绪，必要时启动应用。返回 cdp_port。"""
        def _check_and_maybe_start() -> bool:
            if self.port_open(self.cdp_port) and self.cdp_ready(self.cdp_port):
                if not self.cdp_has_page(self.cdp_port):
                    # 端口可连但无 page 目标：典型为关窗后常驻托盘的残留实例，
                    # 不是可测试的就绪状态，交给 _start() 强杀后重启。
                    logger.info(
                        "[AppProcess] CDP:%s 可连但无 page 目标，按残留实例处理",
                        self.cdp_port,
                    )
                else:
                    proc_info = self.find()
                    if proc_info:
                        self.pid = proc_info["pid"]
                        logger.info(
                            "[AppProcess] %s 已在运行 (PID=%s, CDP=%s)",
                            self.app_name, self.pid, self.cdp_port,
                        )
                        return True
            # 未就绪 → 尝试启动
            self._start()
            return False

        already_ready = await asyncio.to_thread(_check_and_maybe_start)
        if not already_ready:
            await self._wait_cdp(timeout)
        return self.cdp_port

    def kill_by_port(self) -> bool:
        """精确按 CDP 端口杀进程，不影响其他实例。返回是否成功。"""
        if not _HAS_PSUTIL:
            logger.warning("[AppProcess] psutil 不可用，跳过按端口杀进程")
            return False
        killed = False
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                port = self._parse_cdp_port(cmdline)
                if port == self.cdp_port:
                    proc.kill()
                    logger.info("[AppProcess] 已杀进程 PID=%s (CDP=%s)", proc.pid, self.cdp_port)
                    killed = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return killed

    def is_running(self) -> bool:
        """检测应用是否已有实例在运行——**不限定 CDP 端口**（区别于 kill_by_port() /
        find() 的端口精确匹配）。

        用于识别"进程存在但未开调试端口"的遗留/僵尸实例：Electron 应用默认启用
        单实例锁（single-instance lock），若已有一个不带 `--remote-debugging-port`
        参数的实例在运行，之后带该参数再启动的新实例会被系统直接转发给旧实例并
        自行退出——新参数被静默忽略，CDP 端口永远不会打开，进而导致
        `_wait_cdp()` 超时（`CDPTimeoutError`）。必须先探测到这种情况并强制关闭
        旧实例，才能让新实例的调试端口参数生效。
        """
        if not _HAS_PSUTIL:
            return False
        try:
            return bool(self._find_app_procs())
        except Exception:
            return False

    def kill_all(self) -> bool:
        """强制终止**所有**匹配 app_name 的实例（不限定 CDP 端口，是 kill_by_port()
        的超集）。返回是否实际终止了至少一个进程。

        macOS 用 `pkill -x` 精确按进程名匹配（对齐 engine/mac_appkit/engine.py::
        _sync_kill 的成熟实现，避免误杀同名子串的其他进程）；Windows/Linux 用
        psutil 按 `_find_app_procs()`（已排除 Electron Helper 子进程）逐个 kill。
        """
        if sys.platform == "darwin":
            result = subprocess.run(["pkill", "-x", self.app_name], capture_output=True)
            if result.returncode == 0:
                logger.info("[AppProcess] pkill -x '%s' 成功（强制关闭遗留实例）", self.app_name)
                time.sleep(2.0)  # 等待进程完全退出，避免立即重启时端口/单实例锁未释放
                return True
            logger.debug("[AppProcess] pkill -x '%s' 未找到进程（可能已退出）", self.app_name)
            return False

        if not _HAS_PSUTIL:
            logger.warning("[AppProcess] psutil 不可用，无法强制终止 '%s'", self.app_name)
            return False
        killed = False
        try:
            for proc in self._find_app_procs():
                try:
                    psutil.Process(proc["pid"]).kill()
                    logger.info("[AppProcess] 已终止 PID=%s (%s)", proc["pid"], self.app_name)
                    killed = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as exc:
            logger.warning("[AppProcess] kill_all 异常: %s", exc)
        if killed:
            time.sleep(1.5)
        return killed

    def find(self) -> Optional[dict]:
        """查找匹配 cdp_port 的应用主进程，返回 {pid, cdp_port, cmd} 或 None。"""
        if not _HAS_PSUTIL:
            return None
        try:
            candidates = self._find_app_procs()
            for c in candidates:
                if c["cdp_port"] == self.cdp_port:
                    return c
            return candidates[0] if candidates else None
        except Exception:
            return None

    @staticmethod
    def port_open(port: int) -> bool:
        """检查端口是否可连接。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            return s.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def cdp_ready(port: int) -> bool:
        """检查 CDP HTTP 接口（/json/version）是否就绪。"""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=3
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def cdp_has_page(port: int) -> bool:
        """检查 CDP 目标列表（/json/list）中是否存在 type=page 的目标；异常返回 False。"""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=3
            ) as resp:
                if resp.status != 200:
                    return False
                targets = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return False
        return isinstance(targets, list) and any(
            isinstance(t, dict) and t.get("type") == "page" for t in targets
        )

    # ── 内部方法 ────────────────────────────────────────────────

    @staticmethod
    def _detect_app_name(app_path: str) -> str:
        if not app_path:
            return "App"
        m = re.search(r"/([^/]+)\.app(/|$)", app_path)
        if m:
            return m.group(1)
        from pathlib import Path
        return Path(app_path).stem or "App"

    @staticmethod
    def _parse_cdp_port(cmdline: list[str]) -> Optional[int]:
        for arg in cmdline:
            m = re.search(r"--remote-debugging-port=(\d+)", arg)
            if m:
                return int(m.group(1))
        return None

    def _find_app_procs(self) -> list[dict]:
        results = []
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                # 跳过 zombie 进程（macOS 上 SIGKILL 后进程变为 zombie，
                # 父进程未 reap 前 psutil 仍可见，导致 is_running() 误判）
                try:
                    if proc.status() == psutil.STATUS_ZOMBIE:
                        continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                pname = proc.info.get("name") or ""
                exe   = proc.info.get("exe") or ""
                cmdline = proc.info.get("cmdline") or []
                if not self._is_main_proc(pname, exe, cmdline):
                    continue
                port = self._parse_cdp_port(cmdline)
                results.append({
                    "pid": proc.info["pid"],
                    "cdp_port": port,
                    "cmd": " ".join(cmdline),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return results

    def _is_main_proc(self, pname: str, exe: str, cmdline: list[str]) -> bool:
        """判断是否为目标应用主进程（排除 Electron Helper 子进程）。"""
        cmd_str = " ".join(cmdline)
        if "Helper" in pname or "--type=" in cmd_str:
            return False
        if sys.platform == "win32":
            return pname.lower() == f"{self.app_name}.exe".lower()
        elif sys.platform == "darwin":
            return self.app_name in pname and "/MacOS/" in exe
        else:
            return pname == self.app_name

    def _start(self) -> None:
        """带 CDP 端口启动 Electron 应用。

        启动前先检测是否已有遗留实例在运行（is_running()，不限定 CDP 端口）：
        若有，强制关闭（kill_all()，必要时 macOS 用 pkill）后再启动新实例，
        避免 Electron 单实例锁导致 `--remote-debugging-port` 参数被忽略、
        新 CDP 端口永远不会打开（进而 `_wait_cdp()` 超时）。
        """
        if not self.app_path:
            raise AppNotRunningError(
                f"未指定 app_path，且应用未在 CDP:{self.cdp_port} 上运行"
            )
        if not os.path.exists(self.app_path):
            raise AppNotRunningError(
                f"应用可执行文件不存在: {self.app_path}"
            )

        if self.is_running():
            logger.info(
                "[AppProcess] 检测到 '%s' 已有实例在运行（未监听目标 CDP:%s，"
                "可能是遗留进程或单实例锁导致），强制关闭后重新启动",
                self.app_name, self.cdp_port,
            )
            self.kill_all()

        cmd = [self.app_path, f"--remote-debugging-port={self.cdp_port}"]
        cmd.extend(self.extra_args)
        logger.info("[AppProcess] 启动: %s", " ".join(cmd))

        if sys.platform == "win32":
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x00000008 | 0x00000200,  # DETACHED | NEW_PROCESS_GROUP
            )
        else:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        self.launched_by_us = True

    async def _wait_cdp(self, timeout: float = 30.0) -> None:
        """轮询等待 CDP 就绪（0.5s 间隔）。"""
        logger.info("[AppProcess] 等待 CDP:%s 就绪（最多 %.0fs）...", self.cdp_port, timeout)

        def _check() -> bool:
            if self.port_open(self.cdp_port) and self.cdp_ready(self.cdp_port):
                proc_info = self.find()
                if proc_info:
                    self.pid = proc_info["pid"]
                return True
            return False

        elapsed = 0.0
        interval = 0.5
        while elapsed < timeout:
            if await asyncio.to_thread(_check):
                logger.info("[AppProcess] CDP:%s 就绪 (%.1fs)", self.cdp_port, elapsed)
                return
            await asyncio.sleep(interval)
            elapsed += interval

        raise CDPTimeoutError(
            f"CDP:{self.cdp_port} 在 {timeout:.0f}s 内未就绪"
        )
