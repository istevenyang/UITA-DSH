"""LaunchPolicy — per-app 启动后处理策略注册中心。

与 AppRegistry / EngineRegistry / CollectorRegistry 对称的第四件注册中心：
  - AppRegistry:        (app_name, platform) → AppPlatformConfig
  - EngineRegistry:     (app_name, platform) → EngineBase 实现类
  - CollectorRegistry:  (app_name, platform) → LogCollectorBase 实现类
  - LaunchPolicyRegistry: (app_name, platform) → LaunchPolicyBase 实现类

各 App 在 apps/<name>/register.py 中通过 @register_launch_policy 注册自己的
生命周期策略（启动后处理 + 可选的自定义恢复决策），供以下两处消费：
  - core/lifecycle/engine_lifecycle.py 在 launch()/restart() 成功后消费
    （wait_ui_ready + post_launch）。
  - core/lifecycle/app_lifecycle_manager.py 的 recover() 在引擎不健康时消费
    （可选的自定义恢复决策 recover()，2026-07-02 补充）。

**这是"不同 app 的生命周期管理策略"的唯一维护点**：per-app 差异化逻辑集中在
apps/<name>/<name>_launch_policy.py 一个文件里，core/lifecycle/ 内不允许出现
任何 app 特判代码。

设计原则：
  - 未注册的 app：try_get() 返回 None，行为不变（零破坏）。
  - 钩子默认 no-op（wait_ui_ready/post_launch）或返回 None（recover）：
    子类只需重写关心的方法，不重写=完全沿用框架默认行为。
  - 零外部依赖：不 import engine/ 具体实现或 apps/，只依赖标准库（供 core/lifecycle/ 内部消费）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional, Type

from uita.core.registry_common import resolve_universal_platform


class LaunchPolicyBase(ABC):
    """per-app 生命周期策略抽象基类。

    三个钩子均为非抽象方法，子类按需重写：
      - wait_ui_ready: 启动/重启后，等待特定 UI 元素出现，确认应用真正就绪
        （比 EngineBase.is_healthy() 的通用健康检查更精确的业务级信号）。默认 no-op。
      - post_launch:   就绪后的一次性处理（如关闭更新提示、首次弹窗等）。默认 no-op。
      - recover:       [2026-07-02 新增] 引擎不健康时的自定义恢复决策。默认返回 None
        （不覆盖，使用 AppLifecycleManager 的默认算法：软复位优先，失败降级硬重启）。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名称（用于日志标识）。"""
        ...

    async def wait_ui_ready(self, engine: Any) -> None:
        """默认 no-op。子类可重写：等待特定 UI 元素出现。"""
        return

    async def post_launch(self, engine: Any) -> None:
        """默认 no-op。子类可重写：启动后一次性处理（弹窗关闭等）。"""
        return

    async def recover(
        self, engine: Any, default_recover: Callable[[], Awaitable[str]]
    ) -> Optional[str]:
        """默认返回 None（不覆盖）。子类可选择性重写，自定义"引擎不健康"时的恢复决策。

        Args:
            engine: 当前 AppLifecycleManager 持有的引擎实例。
            default_recover: 无参 async 可调用，执行框架默认恢复算法
                （`soft_reset()` 优先，失败降级 `restart()`），返回
                "soft_reset" 或 "hard_restart"。子类可以：
                  1. 不重写（默认返回 None）→ 完全使用默认算法；
                  2. 重写后 `return await default_recover()` → 在默认算法
                     前后插入自定义步骤（如恢复前先关闭遗留弹窗）；
                  3. 重写后完全自定义、不调用 default_recover → 完全接管
                     恢复流程（如某些 app 需要跳过软复位、直接硬重启）。

        Returns:
            None 表示不覆盖（AppLifecycleManager 使用默认算法）；
            否则返回描述实际执行动作的字符串（如 "hard_restart"、
            自定义标签如 "custom_kill_and_relaunch"），供上层日志/断言使用。

        异常处理：由调用方（AppLifecycleManager.recover()）捕获并降级到默认算法，
        本方法内部不需要自行 try/except（保持与 wait_ui_ready/post_launch 一致的
        "钩子异常不阻断主流程"哲学）。
        """
        return None

    async def on_launch_timeout(self, engine: Any) -> bool:
        """[2026-09-01 新增] launch()/restart() 超时后的钩子。

        当 engine.launch() 或 engine.restart() 抛出 RuntimeError（超时）时，
        core/lifecycle/engine_lifecycle.py 会在 re-raise 之前调用本钩子，给
        app 层一次机会处理阻塞超时的根因（如 macOS SecurityAgent 密码授权
        框阻塞了应用主窗口出现）。

        默认返回 ``False``（no-op，不覆盖）。子类可重写：

        - 返回 ``True``：表示已成功处理超时根因（如关闭了阻塞弹窗），
          调用方将重试 ``engine.launch()``。
        - 返回 ``False``：表示未处理 / 处理失败，调用方将 re-raise 原始
          超时异常。

        Args:
            engine: 刚刚 launch/restart 超时的引擎实例。

        异常处理：由调用方（``engine_lifecycle.py::_try_launch_timeout_hook``）
        捕获并降级（仅 warning + re-raise 原始异常），本方法内部不需要自行
        try/except。
        """
        return False

    async def on_hard_restart_ineffective(self, engine: Any) -> bool:
        """[2026-07-21 新增] `clean_launch()` 执行 `restart()` 后，检测到
        `engine.last_kill_effective is False`（kill 未真正生效，如驱动级
        自我保护拦截导致进程/窗口未真正更换，见 docs/plans/
        archive/01-engine-lifecycle-实施纪要.md）
        时的兜底升级动作。

        默认返回 ``False``（no-op，不覆盖，仅记录 warning）。子类可重写：
        尝试更强力的合法退出手段（如 sample_web 的 `quit_via_tray()`），成功后
        应自行重新拉起应用（`engine.launch()`）并返回 ``True``。

        Args:
            engine: 当前硬重启使用的引擎实例（`last_kill_effective` 恰好为
                `False` 时才会被调用）。

        Returns:
            ``True`` 表示已成功处理（升级动作生效），``False`` 表示未处理/
            处理失败（调用方仅记录 warning，不阻断 `clean_launch()` 主流程，
            与 `is_healthy()` 检查解耦，行为与改动前一致）。

        异常处理：由调用方（`clean_launch()`）捕获并降级（仅 warning），
        本方法内部不需要自行 try/except。
        """
        return False


_REGISTRY: dict[tuple[str, str], Type[LaunchPolicyBase]] = {}


class LaunchPolicyRegistry:
    """(app_name, platform) → LaunchPolicyBase 实现类 注册中心。"""

    @staticmethod
    def register(app_name: str, platform: str, policy_cls: Type[LaunchPolicyBase]) -> None:
        """platform 为 "universal" 时，在注册的这一刻解析为当前宿主机真实平台
        （见 core.registry_common.resolve_universal_platform），_REGISTRY 中永远
        只存具体平台 key，try_get() 无需感知 universal 概念。"""
        resolved_platform = resolve_universal_platform(platform)
        key = (app_name.lower(), resolved_platform.lower())
        _REGISTRY[key] = policy_cls

    @staticmethod
    def try_get(app_name: str, platform: str) -> Type[LaunchPolicyBase] | None:
        """查找策略类，未注册时返回 None（不抛异常）。"""
        key = (app_name.lower(), platform.lower())
        return _REGISTRY.get(key)

    @staticmethod
    def all_keys() -> list[tuple[str, str]]:
        return list(_REGISTRY.keys())


def register_launch_policy(app_name: str, platform: str):
    """装饰器：注册 per-app 启动后处理策略。

    用法::

        @register_launch_policy("sample_web", "macOS")
        class SampleLaunchPolicy(LaunchPolicyBase): ...
    """
    def decorator(cls: Type[LaunchPolicyBase]) -> Type[LaunchPolicyBase]:
        LaunchPolicyRegistry.register(app_name, platform, cls)
        return cls
    return decorator
