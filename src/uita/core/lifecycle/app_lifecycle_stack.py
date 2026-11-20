"""AppLifecycleStack — "当前被测 app" 的嵌套栈 + 统一引擎构造工厂。

职责：
  1. build_engine_for_app()：统一的引擎构造工厂，消除 main.py / interpreter.py /
     batch_runner.py 三处重复的 EngineRegistry+AppRegistry+cdp_port 构造逻辑
     （纯提取重构，逻辑零变化）。
  2. AppLifecycleStack：按 (app_name, platform) 缓存 AppLifecycleManager，并维护
     一个嵌套栈——为 main.py 统一路由（当前被测 app）与未来 switch[]/switch_back[]
     （多 app 切换用例）预留接口。

本次范围边界（见计划文档 §3.5）：
  - switch_to()/switch_back() 方法本次完整实现并单测覆盖；
  - 但不新增 DSL `switch[app]`/`switch_back[]` 关键字（留给独立需求）。

计划文档：docs/plans/archive/01-engine-lifecycle-实施纪要.md
"""
from __future__ import annotations

import logging
from typing import Optional

from uita.core.engine.base import EngineBase
from uita.core.engine.registry import EngineRegistry
from uita.core.lifecycle.app_lifecycle_manager import AppLifecycleManager
from uita.core.platform.app_registry import AppRegistry

logger = logging.getLogger("lifecycle.app_stack")


def build_engine_for_app(app_name: str, platform: str) -> EngineBase:
    """统一引擎构造工厂。

    提取自 main.py::_resolve_engine_for_batch 与
    core/runner/batch_runner.py::_ensure_engine，逻辑零变化：
      1. EngineRegistry.get(app_name, platform) 拿引擎类
      2. 尝试用 AppRegistry 中的 cdp_url 解析出 cdp_port（native 引擎无 cdp_url，忽略异常）
      3. 结合 settings.get_app_path() 拿到 app_path，实例化引擎
      4. 引擎构造器不接受 app_path/cdp_port 时（如原生引擎的定制子类），
         TypeError 兜底降级为仅传 cdp_url

    2026-07-27 新增：同步透传 AppRegistry.extra 中的 url/channel/storage_state
    （非空才透传，缺失的 app 走空 dict，构造参数与改造前逐字节一致），供
    Web Playwright 独立浏览器启动模式使用，见
    docs/plans/archive/01-engine-lifecycle-实施纪要.md。
    """
    from uita.config import BASE_CDP_PORT, get_app_path

    engine_cls = EngineRegistry.get(app_name, platform)

    cdp_port = BASE_CDP_PORT
    extra_kwargs: dict = {}
    try:
        cfg = AppRegistry.get(app_name, platform)
        cdp_port = int(cfg.cdp_url.split(":")[-1]) if cfg.cdp_url else BASE_CDP_PORT
        # Web Playwright 独立启动所需字段透传（2026-07-27 新增）：url/channel/
        # storage_state 缺失时为空串，PlaywrightEngine 按空串走原 CDP attach
        # 路径，零回归。见
        # docs/plans/archive/01-engine-lifecycle-实施纪要.md。
        # process_name 透传（2026-07-28 新增，供 run_exe[] ephemeral 注册指定
        # 真实进程名，见 core/lifecycle/ephemeral_app.py）：缺失时为空串，
        # WinUIAEngine/MacAppKitEngine 未收到该 kwarg 时回退自身默认逻辑
        # （process_name or app_name），零回归。见
        # docs/plans/archive/01-engine-lifecycle-实施纪要.md。
        extra_kwargs = {
            k: v for k, v in {
                "url": cfg.extra.get("url", ""),
                "channel": cfg.extra.get("channel", ""),
                "storage_state": cfg.extra.get("storage_state", ""),
                "start_maximized": cfg.extra.get("start_maximized", False),
                "process_name": cfg.extra.get("process_name", ""),
                "launch_args": cfg.extra.get("launch_args", []),
                "window_title": cfg.extra.get("window_title", ""),
                "window_class_name": cfg.extra.get("class_name", ""),
                "preserve_existing_processes": cfg.extra.get("preserve_existing_processes", False),
                "force_launch": cfg.extra.get("force_launch", False),
            }.items() if v
        }
    except Exception:
        pass

    app_path = get_app_path(app_name)
    try:
        # platform / app_name kwarg 供跨平台、跨 app 复用的引擎（如 PlaywrightEngine
        # 同时服务 macos+windows 与多个 Electron app）正确写入实例属性，
        # 供 LaunchPolicy/Resolver/LogCollector 等 (app_name, platform) 路由组件匹配。
        # 2026-07-06：PlaywrightEngine 跨平台注册后新增 platform 入参；
        # 2026-07-06：app_name 实例属性化（支持 switch[] 切换到其他 app 复用同引擎类）。
        return engine_cls(
            cdp_url=f"http://localhost:{cdp_port}",
            app_path=app_path,
            cdp_port=cdp_port,
            platform=platform,
            app_name=app_name,
            **extra_kwargs,
        )
    except TypeError:
        # 引擎 __init__ 不接受 platform/app_name/extra_kwargs（如 MacAppKitEngine/
        # WinUIAEngine 等单平台原生引擎，它们 __init__ 已有独立的 app_name 参数处理），
        # 降级为仅传 cdp_url，与改造前完全一致。
        return engine_cls(cdp_url=f"http://localhost:{cdp_port}")


class AppLifecycleStack:
    """"当前被测 app" 的嵌套栈。

    - _cache：(app_name, platform) → AppLifecycleManager，同一 app 全程复用同一
      engine 实例（避免重复构造/重复冷启动）。
    - _stack：当前激活的 manager 栈，栈顶即"当前被测 app"。switch_to() 只 push
      不 pop，switch_back() pop 一层——天然支持多层嵌套切换
      （appX → switch[appY] → switch[appZ] → switch_back[] 回到 appY → ...）。
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], AppLifecycleManager] = {}
        self._stack: list[AppLifecycleManager] = []

    @property
    def current(self) -> Optional[AppLifecycleManager]:
        return self._stack[-1] if self._stack else None

    def _key(self, app_name: str, platform: str) -> tuple[str, str]:
        return (app_name.lower(), platform.lower())

    async def _bring_to_foreground(self, mgr: AppLifecycleManager, tag: str) -> None:
        """切换 context 后尽力将原生窗口置前台；未实现该钩子的引擎保持原行为。"""
        foreground_fn = getattr(mgr.engine, "bring_to_foreground", None)
        if foreground_fn is None:
            return
        try:
            await foreground_fn()
        except Exception as exc:
            logger.debug("[%s] bring_to_foreground 失败（忽略）: %s", tag, exc)

    async def get_or_create(self, app_name: str, platform: str) -> AppLifecycleManager:
        """按 (app_name, platform) 缓存 manager，重复调用返回同一实例。"""
        key = self._key(app_name, platform)
        mgr = self._cache.get(key)
        if mgr is None:
            engine = build_engine_for_app(app_name, platform)
            mgr = AppLifecycleManager(app_name, platform, engine)
            self._cache[key] = mgr
            logger.debug("[stack] 新建 manager: %s/%s", app_name, platform)
        return mgr

    async def enter(
        self,
        app_name: str,
        platform: str,
        *,
        case_name: str = "",
        is_first: bool = False,
        tag_prefix: str = "lifecycle",
    ) -> AppLifecycleManager:
        """获取/复用 manager → prepare_for_case() → push 入栈（若不是当前栈顶）。

        main.py / interpreter.py / batch_runner.py 的统一入口调用此方法。
        """
        mgr = await self.get_or_create(app_name, platform)
        await mgr.prepare_for_case(case_name=case_name, is_first=is_first, tag_prefix=tag_prefix)
        if self.current is not mgr:
            self._stack.append(mgr)
        return mgr

    async def switch_to(self, app_name: str, platform: str, *, tag_prefix: str = "switch") -> AppLifecycleManager:
        """[REQ-003 switch[]] 切换到新 app（引擎池语义）。

        get_or_create() 复用/构造 manager 后统一调用 ensure_alive()——健康
        直接复用；不健康先尝试重新附着（launch()，不 kill）；仍不健康（进程
        确已不可用）才降级 recover()（软复位/硬重启）。不再区分"缓存命中/
        未命中"走不同路径，从根本上消除"未缓存 app 首次 switch 命中
        RESTART_BEFORE_FIRST_CASE 强制硬重启"误杀已存在健康进程的问题。

        push 入栈，不影响之前的 context（可通过 switch_back() 恢复）。

        与"用例间"生命周期（enter()/prepare_for_case()，受
        RESTART_BEFORE_FIRST_CASE/RESTART_BETWEEN_CASES 控制）职责完全分离，
        互不影响。

        计划文档：docs/plans/archive/02-dsl-actions-实施纪要.md
        （前身：docs/plans/archive/02-dsl-actions-实施纪要.md）
        """
        mgr = await self.get_or_create(app_name, platform)
        tag = f"{tag_prefix}[{app_name}]"
        action = await mgr.ensure_alive(tag=tag)
        await self._bring_to_foreground(mgr, tag)
        if self.current is not mgr:
            self._stack.append(mgr)
        logger.info(
            "[stack] switch_to: %s/%s → %s（引擎池保活，未强制 kill）",
            app_name, platform, action,
        )
        return mgr

    async def switch_back(self) -> Optional[AppLifecycleManager]:
        """[REQ-004 switch_back[]] 弹出当前 context，恢复到上一个 app。

        对恢复后的上一个 app 调用 ensure_alive()（保活优先：健康检查 →
        不健康则重新附着，不 kill → 仍不健康才降级 recover()），确保切回后
        仍可用且不会误杀正常运行的引擎。栈仅剩 1 层（或空）时不弹出，直接
        返回当前 manager。
        """
        if len(self._stack) <= 1:
            return self.current
        popped = self._stack.pop()
        prev = self.current
        logger.info("[stack] switch_back: %s → %s", popped.app_name, prev.app_name if prev else "?")
        if prev is not None:
            await prev.ensure_alive(tag="switch_back")
            await self._bring_to_foreground(prev, "switch_back")
        return prev

    async def evict(self, app_name: str, platform: str) -> None:
        """移除 (app_name, platform) 的缓存 manager：终止进程（引擎支持
        ``terminate()`` 时优先调用，否则降级 ``close()``）+ 从 _cache/_stack
        彻底移除。

        与 close_all() 不同：只清一条。供 run_exe[] 场景使用——
          1. 同一 alias 复用时发现 exe 路径变化，需先清旧的再冷启动；
          2. switch_back[cleanup=true] 对刚弹出的 ephemeral app 做清理；
          3. sweep_ephemeral_apps() 兜底清理遗留的 ephemeral 条目。

        未命中缓存时静默返回，不报错。

        计划文档：docs/plans/archive/01-engine-lifecycle-实施纪要.md
        """
        key = self._key(app_name, platform)
        mgr = self._cache.pop(key, None)
        if mgr is None:
            return
        self._stack = [m for m in self._stack if m is not mgr]
        terminate_fn = getattr(mgr.engine, "terminate", None)
        try:
            if callable(terminate_fn):
                await terminate_fn()
            else:
                await mgr.close()
        except Exception as exc:
            logger.warning("[stack] evict(%s/%s) 清理异常: %s", app_name, platform, exc)

    def register_root(
        self, app_name: str, platform: str, engine: Optional[EngineBase] = None,
    ) -> AppLifecycleManager:
        """把当前 case/batch 已构造好的"根"引擎登记进 `_cache` +
        `_stack`（若尚未在栈内）。

        背景（真机验证发现的 bug）：`switch[]`/`switch_back[]`（M10）与
        `run_exe[]`（本次）都依赖 `_stack` 判断"是否还有上一层可切回"
        （`switch_back()` 内 `len(self._stack) <= 1` 视为栈底，不弹出）。但
        `runner` 模式的 `core/interpreter.py::_build_engine()`、`batch` 模式的
        `main.py::_resolve_engine_for_batch()`、`batch` 模式的
        `core/runner/batch_runner.py::_ensure_engine()` 三处获取"当前被测
        app"根引擎时，均从未把它 push 进 `_stack`（`_stack` 从空开始）——
        导致 case 内第一次 `switch[]`/`run_exe[]` 后栈长度变成 1，
        `switch_back()` 误判为"无上一个可切回"，实际应切回的是这个从未入栈的
        根 app。真机验证：`run_exe[cpu-z.exe]` 后 `switch_back[cleanup=true]`
        未能真正切回 web 引擎（日志显示"switched back to cpu-z..."而非
        sample_web）。见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md §11。

        Args:
            engine: 仅在 `(app_name, platform)` 未缓存时使用（用于包裹调用方
                已构造好的引擎实例，避免调用 `build_engine_for_app()` 重复
                构造/重复冷启动 Playwright 浏览器等有状态资源）；若已缓存
                （如 batch 模式先调用 `get_or_create()`），忽略该参数直接复用。

        Idempotent：多次调用（batch 模式跨多条 case 复用同一根引擎）
        只在首次真正 push，其余调用直接返回已缓存的 manager，不重复入栈。
        """
        key = self._key(app_name, platform)
        mgr = self._cache.get(key)
        if mgr is None:
            if engine is None:
                raise ValueError(
                    f"register_root: ({app_name}, {platform}) 未缓存且未提供 engine"
                )
            mgr = AppLifecycleManager(app_name, platform, engine)
            self._cache[key] = mgr
        if mgr not in self._stack:
            self._stack.append(mgr)
        return mgr

    async def close_all(self) -> None:
        """清理所有已创建 manager 的 engine，清空栈与缓存（进程退出/测试收尾用）。"""
        for mgr in self._cache.values():
            await mgr.close()
        self._stack.clear()
        self._cache.clear()


_default_stack: Optional[AppLifecycleStack] = None


def get_default_stack() -> AppLifecycleStack:
    """进程级单例，main.py 各模式共用同一嵌套栈。"""
    global _default_stack
    if _default_stack is None:
        _default_stack = AppLifecycleStack()
    return _default_stack


def reset_default_stack() -> None:
    """仅供测试重置单例，避免测试间状态串染。"""
    global _default_stack
    _default_stack = None
