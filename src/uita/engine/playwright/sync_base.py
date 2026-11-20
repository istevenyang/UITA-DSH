"""
SyncEngineBase — bridge for synchronous automation libraries (atomacos, pywinauto).

Mac/Win engines inherit this and wrap sync calls in asyncio.to_thread().
This is a skeleton for Day 5 / future extension; not used in the Demo.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional, TypeVar

from uita.core.engine.base import EngineBase
from uita.core.engine.sync_bridge import BoundedExecutor

T = TypeVar("T")


class SyncEngineBase(EngineBase):
    """
    Mixin that provides _run_sync() for wrapping blocking calls.

    Usage in subclass:
        async def click(self, element):
            await self._run_sync(self._sync_click, element)

        def _sync_click(self, element):
            element.raw.click()  # blocking

    2026-08-21 修复（见计划文档 20260820160000-mac-win-engine-screenshot-
    hang-isolation-and-shared-bounded-executor.md）：原实现用
    ``loop.run_in_executor(None, ...)``——``None`` 即进程级共享默认执行器，
    该 mixin 唯一使用者 ``MacAppKitEngine`` 的全部操作（click/type/
    find_by_id/is_healthy/get_element_rect/take_screenshot 等 30+ 调用点）
    都走这一条路径，一次阻塞调用卡死会拖慢进程内其他所有引擎实例/其他调用
    方的 run_in_executor 调用（与已修复的 PlaywrightEngine L3 是同一类架构
    缺陷）。现改用实例私有的 ``BoundedExecutor``，隔离故障面；``timeout=None``
    （默认，现有全部调用点不传新参数）时行为与改动前逐字节等价（仅执行域从
    共享池换成私有池，不改变任何返回值/异常/耗时语义）。
    """

    async def _run_sync(
        self, fn: Callable[..., T], *args, timeout: Optional[float] = None, **kwargs,
    ) -> T:
        """Run a blocking function in an instance-private bounded executor.

        ``timeout`` 为 ``None``（默认）时行为与改动前完全一致：无限等待，
        仅执行域从共享默认池换成实例私有池（纯粹的故障隔离，不改变行为）。
        传入具体秒数时，超时后放弃等待并返回 ``None``（不抛异常），调用方
        （如 ``take_screenshot()``）需自行处理该"失败"信号。
        """
        guard = self._get_sync_guard()
        bound_fn = lambda: fn(*args, **kwargs)
        if timeout is None:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(guard.get(), bound_fn)
        return await guard.run(bound_fn, timeout=timeout)

    def _get_sync_guard(self) -> BoundedExecutor:
        """惰性创建实例私有 ``BoundedExecutor``（mixin 无 ``__init__``，用
        ``getattr`` 模式实现首次使用时创建实例属性，对未来任何新的
        ``SyncEngineBase`` 子类零侵入，不要求改造 ``__init__``）。
        """
        guard = getattr(self, "_sync_guard", None)
        if guard is None:
            guard = BoundedExecutor(max_workers=4, thread_name_prefix="sync-engine")
            self._sync_guard = guard
        return guard

    async def _shutdown_sync_guard(self) -> None:
        """引擎 ``close()`` 时调用，清理私有执行器。未创建过 guard 时安全
        跳过，不报错。
        """
        guard = getattr(self, "_sync_guard", None)
        if guard is not None:
            guard.shutdown()
