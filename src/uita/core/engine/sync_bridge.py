"""
BoundedExecutor — 通用「隔离执行域 + 有界等待 + 可恢复」同步调用桥接器。

供 EngineBase 具体实现（Playwright/mac_appkit/win_uia）统一复用，替代各引擎
散落的 ``loop.run_in_executor(None, ...)``（进程共享默认池，一次阻塞调用卡死
会拖慢进程内其他所有引擎实例/其他调用方）或各自手写的私有线程池管理代码。

计划文档：docs/plans/20260820160000-mac-win-engine-screenshot-hang-isolation-
and-shared-bounded-executor.md

设计原则：
  - 零外部依赖：不 import 任何具体引擎、不 import EngineBase，纯通用工具类，
    供 engine/ 各子目录以「组合」方式持有一个实例，不改变任何继承结构。
  - run() 超时返回 None（不抛异常）：调用方按"本次调用失败"的既有语义处理
    （如截图失败返回 b""/None），不引入新的异常传播路径。
  - 超时后底层线程可能仍在后台运行（Python 无法强行终止一个已启动的同步
    线程）——本类只解决"隔离故障面"（不拖慢其他调用），不解决"强制终止单次
    调用内部"。需要"真正可 kill"语义的场景（如 macOS screencapture 子进程）
    应改用 asyncio.create_subprocess_exec，与本类是互补关系，非替代关系。

用法::

    class SomeEngine:
        def __init__(self):
            self._guard = BoundedExecutor(max_workers=2, thread_name_prefix="xxx")

        async def some_blocking_call(self):
            result = await self._guard.run(self._sync_impl, arg1, timeout=10)
            return result if result is not None else b""

        async def close(self):
            self._guard.shutdown()
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class BoundedExecutor:
    """惰性创建的私有线程池 + 有界等待 + 故障恢复（respawn/shutdown）。"""

    def __init__(
        self,
        max_workers: int = 1,
        thread_name_prefix: str = "",
        initializer: Optional[Callable[[], None]] = None,
        name: str = "",
    ) -> None:
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._initializer = initializer
        self._name = name or thread_name_prefix or "bounded-executor"
        self._executor: Optional[ThreadPoolExecutor] = None
        self._lock = threading.Lock()

    def get(self) -> ThreadPoolExecutor:
        """惰性创建/复用底层线程池（懒加载单例，线程安全）。"""
        if self._executor is None:
            with self._lock:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self._max_workers,
                        thread_name_prefix=self._thread_name_prefix,
                        initializer=self._initializer,
                    )
        return self._executor

    async def run(self, fn: Callable[..., Any], *args: Any, timeout: float) -> Any:
        """在隔离执行域内运行 ``fn(*args)``，超过 ``timeout`` 秒未完成则放弃
        等待并返回 ``None``（不抛异常）。

        底层线程可能仍在后台运行，但因为隔离在本执行域的私有池子里，不会
        拖慢共享默认执行器或其他 BoundedExecutor 实例的调用。
        """
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self.get(), fn, *args), timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] 阻塞调用超时(%ss)，已放弃等待（该调用可能仍在隔离执行域"
                "内后台运行，不影响其他调用）", self._name, timeout,
            )
            return None

    def respawn(self) -> None:
        """放弃当前执行域（含可能仍卡死的线程），下次 get()/run() 惰性重建
        全新线程池。

        用于 max_workers=1 的单线程串行场景（如 Windows STA 执行器）：一次
        超时后卡死的线程本身无法被强行杀死，唯一恢复手段是放弃这个执行域
        整体，用全新线程接管后续任务，使调用方从"永久排队等待一个不会释放
        的 worker"恢复为"可继续正常工作"。
        """
        old, self._executor = self._executor, None
        if old is not None:
            old.shutdown(wait=False)

    def shutdown(self) -> None:
        """引擎 close() 时调用，清理线程池，避免进程退出被非 daemon 线程阻塞。

        未创建过执行域（``get()``/``run()`` 从未被调用）时安全跳过，不报错。
        """
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # Python <3.9 无 cancel_futures 参数，降级兜底
                self._executor.shutdown(wait=False)
            self._executor = None
