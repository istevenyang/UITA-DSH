"""run_exe[] 专用：运行时动态注册"临时原生 app"（ephemeral native app）。

背景：run_exe[<path>] 需要在 case 运行时（而非 apps/<name>/register.py 的 import
时）临时注册一个指向某个本地 exe 的 (alias, platform) → AppPlatformConfig +
EngineRegistry 映射，复用当前 case 所属 app 的 rosetta_map.json（同一份文件混排
web selector/js 字段与 exe 的 win/mac 字段），然后借助已有的
AppLifecycleStack.switch_to() 完成引擎构造/复用判定/生命周期管理——不改动
AppRegistry/EngineRegistry/Resolver/ElementMapLoader 任何一行。

AppPlatformConfig.extra["ephemeral"] = True 是本模块注册的条目的统一标记，供
sweep_ephemeral_apps()（下沉到 core/lifecycle/engine_lifecycle.py::
prepare_engine_for_case()，每条 case 开始前兜底清理）与
core/actions/common/action_switch.py::SwitchBackAction 的 cleanup=true 分支识别。

计划文档：docs/plans/archive/01-engine-lifecycle-实施纪要.md
"""
from __future__ import annotations

import logging
import ntpath
import os

from uita.core.actions.registry import register_action_scope_alias
from uita.core.engine.registry import EngineRegistry
from uita.core.platform.app_registry import AppNotFoundError, AppPlatformConfig, AppRegistry, LaunchStrategy

logger = logging.getLogger("lifecycle.ephemeral_app")


class EphemeralAppConflictError(Exception):
    """alias 与已静态注册的正式 app（非 ephemeral 标记）冲突，拒绝覆盖。"""


def _native_engine_cls(platform: str):
    """按平台选原生引擎类（run_exe[] 仅支持 Windows/macOS 原生 UIA）。"""
    plat = platform.lower()
    if plat == "windows":
        from uita.engine.win_uia.engine import WinUIAEngine
        return WinUIAEngine
    if plat == "macos":
        from uita.engine.mac_appkit.engine import MacAppKitEngine
        return MacAppKitEngine
    raise ValueError(f"run_exe[] 不支持平台 '{platform}'（仅支持 windows/macos 原生 UIA）")


async def register_ephemeral_native_app(
    alias: str,
    exe_path: str,
    platform: str,
    source_app_name: str,
    launch_args: list[str] | None = None,
) -> None:
    """动态注册/复用判定核心逻辑（协程，供 action_run_exe.py 在 case 执行的
    异步上下文中直接 await 调用）。

    Args:
        alias:           run_exe[..., as=<alias>] 指定的临时 app 别名。
        exe_path:         已解析的绝对路径（真实存在）。
        platform:         当前 case 运行平台（"windows"/"macos"）。
        source_app_name:  当前 case 所属的真实 app 名（用于复用其 rosetta_map_path）。

    Raises:
        EphemeralAppConflictError: alias 与已注册的非 ephemeral（正式静态注册）
            app 冲突。
    """
    from uita.core.lifecycle.app_lifecycle_stack import get_default_stack

    key_plat = platform.lower()

    try:
        existing_cfg = AppRegistry.get(alias, platform)
    except AppNotFoundError:
        existing_cfg = None

    if existing_cfg is not None:
        if not existing_cfg.extra.get("ephemeral"):
            raise EphemeralAppConflictError(
                f"alias='{alias}' 与已注册的正式 app 冲突，请在 run_exe[] 中"
                f"改用 as=<其他别名>"
            )
        if existing_cfg.extra.get("source_exe_path") == exe_path:
            register_action_scope_alias(alias, source_app_name)
            logger.debug(
                "[ephemeral] alias='%s' 路径未变('%s')，跳过重新注册，交由 "
                "switch_to() 走 recover() 复用", alias, exe_path,
            )
            return
        # 路径变化：视为该 alias 被重新绑定到另一个 exe，先清理旧引擎/进程，
        # 不能直接 recover()（否则健康检查会对着错误的进程做无意义判断）。
        logger.info(
            "[ephemeral] alias='%s' 路径变化('%s' → '%s')，先 evict 旧条目",
            alias, existing_cfg.extra.get("source_exe_path"), exe_path,
        )
        await get_default_stack().evict(alias, platform)

    # 2026-07-29 修复：改用 ntpath（而非 os.path）—— exe_path 语义上始终是
    # Windows 路径（UIA 原生引擎），但单测/开发机常在 macOS/Linux 上运行；
    # os.path 在 posix 主机上不识别 "\\" 分隔符，会把整串路径当成文件名返回。
    # ntpath 同时兼容 "\\" 与 "/"，对纯 "/" 路径行为与 os.path.basename 一致，
    # 是严格超集，不引入回归（同款修复见 action_run_exe.py::_default_alias()）。
    process_name = ntpath.splitext(ntpath.basename(exe_path))[0]
    rosetta_map_path = ""
    if source_app_name:
        try:
            rosetta_map_path = AppRegistry.get(source_app_name, platform).rosetta_map_path
        except AppNotFoundError:
            logger.warning(
                "[ephemeral] source_app_name='%s' 未注册，无法复用其 "
                "rosetta_map_path，run_exe[] 元素解析可能找不到映射", source_app_name,
            )

    AppRegistry.register(AppPlatformConfig(
        app_name=alias,
        platform=platform,
        launch_strategy=(
            LaunchStrategy.NATIVE_WIN if key_plat == "windows" else LaunchStrategy.NATIVE_MAC
        ),
        rosetta_map_path=rosetta_map_path,
        extra={
            "ephemeral": True,
            "source_exe_path": exe_path,
            "process_name": process_name,
            "launch_args": list(launch_args or []),
            "preserve_existing_processes": True,
            "force_launch": True,
        },
    ))
    register_action_scope_alias(alias, source_app_name)

    if (alias.lower(), key_plat) not in EngineRegistry.all_keys():
        EngineRegistry.register(alias, platform, _native_engine_cls(platform))

    # app_path 走现成的 settings.get_app_path() env 机制（UITA_<APP>_PATH），
    # 进程内设置即可，不落盘 .env.local（ephemeral 本质无需跨进程持久化）。
    os.environ[f"UITA_{alias.upper()}_PATH"] = exe_path


async def sweep_ephemeral_apps(*, exclude_engine=None) -> None:
    """遍历 AppLifecycleStack 缓存，evict 所有 extra.ephemeral=True 的条目。

    调用时机：`prepare_engine_for_case()` 头部（每次为某个引擎做 case 准备前）。
    runner/batch 三态均严格顺序执行单条 case（batch 侧
    core/runner/batch_runner.py::run() 亦为单进程顺序 while 循环，
    非并发），时序无竞争。

    Args:
        exclude_engine: 本次 `prepare_engine_for_case()` 正在准备的引擎实例
            （跳过对它自身的清理）。**必须传入**——否则会出现真机验证发现的
            自我清理 bug：`run_exe[]` 内部 `AppLifecycleStack.enter()` 流程是
            "先 `get_or_create()` 写入 `_cache`（此时已标记 `ephemeral=True`）
            → 再调用 `mgr.prepare_for_case()`"，若不排除自身，本函数会在这个
            刚创建、**还未真正启动**的临时引擎自己的首次 `prepare_for_case()`
            阶段就把它从 `_cache` 中 evict 掉（此时 kill 是空转 no-op，因为
            进程尚未拉起）——导致后续 `switch_back[cleanup=true]` 真正想清理
            该进程时，`_cache` 中已找不到这条记录，`AppLifecycleStack.evict()`
            静默空返回，真实进程未被杀死（但调用方日志仍误报"已 evict"，
            与 `docs/plans/20260725110000-device-status-card-popup-
            window.md`"踩坑记录"同类问题）。见
            docs/plans/20260728120000-run-exe-native-uia-cross-engine-
            switch.md §11。
    """
    from uita.core.lifecycle.app_lifecycle_stack import get_default_stack

    stack = get_default_stack()
    for key in list(stack._cache.keys()):  # noqa: SLF001
        app_name, plat = key
        mgr = stack._cache.get(key)  # noqa: SLF001
        if mgr is None:
            continue
        if exclude_engine is not None and mgr.engine is exclude_engine:
            continue  # 正在被准备的引擎自身，跳过（不能自我清理，见上）
        try:
            cfg = AppRegistry.get(app_name, plat)
        except AppNotFoundError:
            continue
        if cfg.extra.get("ephemeral"):
            logger.info("[ephemeral] sweep: 清理遗留 ephemeral app %s/%s", app_name, plat)
            await stack.evict(app_name, plat)
