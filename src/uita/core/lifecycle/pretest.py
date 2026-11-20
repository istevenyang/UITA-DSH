"""Pretest 数据构造编排器。

由 main.py --mode pretest 调用，职责仅限于：定位并调用
apps/<app_name>/pre_test.py::main(ctx)，附加超时保护与异常兜底。

具体要构造什么数据、目录/DB 如何隔离、是否需要先删后建、是否需要
查重跳过等，完全由各 App 自己的 pre_test.py 决定——本模块不提供、
不假设、不强制任何"标准数据构造流程"，避免框架对业务数据形态做
不必要的假设（相关最佳实践建议见
`.dsh/knowledge/common/policy/pretest-data-preparation.md`，
仅作参考，不由本模块代码强制）。

构造成功或失败均不阻断后续测试流程：pretest 与 batch/runner
是通过 CLI 顺序执行的独立进程调用（如 `pretest` 后接 `batch`），
本模块内部不做、也不应该做跨 mode 的进程内阻断决策，只需如实返回
执行结果，由调用方（`main.py::_handle_pretest`）如实记录日志/输出。
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from uita.core.platform.app_registry import AppNotFoundError, AppRegistry
from uita.core.registry_common import UNIVERSAL_PLATFORM, resolve_universal_platform
from uita.config import ROOT_DIR

logger = logging.getLogger("uita.pretest")

# 单次 pretest 执行超时（秒），可用环境变量 UIA_PRETEST_TIMEOUT_SEC 覆盖。
_DEFAULT_TIMEOUT_SEC = 300.0


@dataclass
class PretestResult:
    """数据构造结果。status 取值：success / skipped / failed。"""

    status: str
    message: str = ""


@dataclass
class PretestContext:
    """注入 pre_test.py::main 的上下文。"""

    app_name: str
    platform: str
    extra: dict = field(default_factory=dict)
    root_dir: str = ROOT_DIR


def _get_timeout_sec() -> float:
    raw = os.environ.get("UIA_PRETEST_TIMEOUT_SEC")
    if not raw:
        return _DEFAULT_TIMEOUT_SEC
    try:
        value = float(raw)
        return value if value > 0 else _DEFAULT_TIMEOUT_SEC
    except ValueError:
        logger.warning("UIA_PRETEST_TIMEOUT_SEC=%r 非法，使用默认值 %s", raw, _DEFAULT_TIMEOUT_SEC)
        return _DEFAULT_TIMEOUT_SEC


def _load_pretest_module(app_name: str) -> tuple[Any | None, Optional[str]]:
    """动态加载 apps/<app_name>/pre_test.py，与项目既有惯例
    （见 main.py::_auto_discover / core/result/result_sync.py）保持一致，
    统一走 importlib.import_module，确保 pre_test.py 内部的绝对导入
    （如 `from apps.<app>.configs import xxx`）能正常工作。

    返回 (module, load_error)：
    - (module, None)：加载成功；
    - (None, None)：apps/<app_name>/pre_test.py 本身不存在 —— 视为 skipped；
    - (None, "<msg>")：模块存在但加载失败（语法错误/内部 import 缺失等）
      —— 是真实 bug，必须视为 failed，不能被静默吞成 skipped。
    """
    module_name = f"apps.{app_name}.pre_test"
    try:
        module = importlib.import_module(module_name)
        return module, None
    except ModuleNotFoundError as e:
        if e.name == module_name:
            logger.info("App %s 未提供 pre_test.py，跳过 pretest", app_name)
            return None, None
        msg = f"加载 pre_test.py 失败（内部 import 缺失依赖 {e.name}）: {e}"
        logger.error(msg)
        return None, msg
    except Exception as e:  # noqa: BLE001
        msg = f"加载 pre_test.py 失败: {e}\n{traceback.format_exc()}"
        logger.error(msg)
        return None, msg


def _build_context(app_name: str, platform: str) -> PretestContext:
    """构造 PretestContext，extra 优先取 AppRegistry 中已注册配置。"""
    extra: dict = {}
    try:
        cfg = AppRegistry.get(app_name, platform)
        extra = dict(cfg.extra)
    except AppNotFoundError:
        logger.debug("App %s platform %s 未注册，extra 为空", app_name, platform)

    return PretestContext(
        app_name=app_name,
        platform=platform,
        extra=extra,
        root_dir=ROOT_DIR,
    )


def _run_sync_with_timeout(main_fn: Callable[[Any], Any], ctx: PretestContext, timeout_sec: float) -> Any:
    """在守护线程中执行同步 main_fn，超时抛 TimeoutError。

    使用 daemon 线程而非 ThreadPoolExecutor：ThreadPoolExecutor.shutdown() 默认会
    等待任务线程结束才能退出上下文，若 main_fn 卡死会导致进程无法退出；
    daemon 线程超时后放弃等待即可让主流程继续，卡死的线程随进程退出被回收。
    """
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = main_fn(ctx)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        raise TimeoutError(f"pre_test.py::main 执行超时（>{timeout_sec}s），底层线程已放弃等待")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def run_pretest(app_name: str, platform: Optional[str] = None) -> PretestResult:
    """执行单个 App 的 pretest 数据构造。

    仅负责定位、调用 apps/<app_name>/pre_test.py::main(ctx)，具体构造什么
    数据、目录/DB 如何隔离由该 App 自行决定，本函数不介入、不校验。

    任何异常都会被捕获并转换为 PretestResult(status="failed")，不会向上抛，
    也不会阻断调用方继续执行后续模式（如 batch）。
    """
    if not app_name:
        logger.warning("app_name 为空，跳过 pretest")
        return PretestResult("skipped", "app_name is empty")

    effective_platform = platform or resolve_universal_platform(UNIVERSAL_PLATFORM)
    ctx = _build_context(app_name, effective_platform)

    module, load_error = _load_pretest_module(app_name)
    if load_error is not None:
        return PretestResult("failed", load_error)
    if module is None:
        return PretestResult("skipped", f"apps/{app_name}/pre_test.py not found")

    main_fn = getattr(module, "main", None)
    if not callable(main_fn):
        logger.warning("pre_test.py 未暴露 main 函数，跳过: %s", app_name)
        return PretestResult("skipped", "pre_test.py has no callable main")

    timeout_sec = _get_timeout_sec()
    logger.info(
        "开始执行 pretest: app=%s platform=%s timeout=%ss",
        app_name, effective_platform, timeout_sec,
    )
    try:
        if inspect.iscoroutinefunction(main_fn):
            result = asyncio.run(asyncio.wait_for(main_fn(ctx), timeout=timeout_sec))
        else:
            result = _run_sync_with_timeout(main_fn, ctx, timeout_sec)

        if not isinstance(result, PretestResult):
            logger.warning("pre_test.py::main 返回值不是 PretestResult，视作 success: %s", type(result))
            return PretestResult("success", "main returned non-PretestResult")
        return result
    except (asyncio.TimeoutError, TimeoutError) as e:
        msg = f"执行 pre_test.py::main 超时（>{timeout_sec}s）: app={app_name}, {e}"
        logger.error(msg)
        return PretestResult("failed", msg)
    except Exception as e:  # noqa: BLE001
        msg = f"执行 pre_test.py::main 异常: {e}\n{traceback.format_exc()}"
        logger.error(msg)
        return PretestResult("failed", msg)
