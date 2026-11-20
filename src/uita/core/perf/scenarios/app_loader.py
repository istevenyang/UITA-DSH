"""按约定加载 app 侧 perf 场景包（动态 import，守 R2）。

约定：app 的 perf 场景住在 ``apps/<app>/perf/scenarios/``，**导入即注册**
（包 ``__init__`` 里对每个场景模块调 ``register_scenario``）——
与 ``apps/<app>/actions.py`` 用 ``@register_action`` 注册同款思路。

为什么用**动态** import：宪法 R2 禁止 ``core/`` 静态 ``import apps.xxx``
（core 不得反向依赖具体 app），故这里按 app 名拼模块路径延迟导入，
先例见 ``core/test_hub/sync.py``。

"该 app 没有场景包"是**常态**（只有实现了性能场景的 app 才会建这个包），
故 ``ModuleNotFoundError`` 命中目标包自身时按跳过处理；
但包内子模块缺失（如场景文件里的 import 写错）会**原样抛出**，不静默吞掉。
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Optional

from uita.core.perf.presets import coerce_name_filter
from uita.core.perf.scenarios.registry import available_scenarios

logger = logging.getLogger("uita")

__all__ = [
    "APP_DEFAULTS_ATTR",
    "DEFAULT_LAUNCH_PROBE_MODULE_TEMPLATE",
    "DEFAULT_PRESETS_MODULE_TEMPLATE",
    "DEFAULT_ROUND_PREPARE_MODULE_TEMPLATE",
    "DEFAULT_ROUND_TEARDOWN_MODULE_TEMPLATE",
    "DEFAULT_RUN_SETUP_MODULE_TEMPLATE",
    "DEFAULT_SCENARIOS_MODULE_TEMPLATE",
    "DEFAULT_STARTUP_READY_MODULE_TEMPLATE",
    "CROSS_UNIT_CHARTS_ATTR",
    "DEFAULT_PRESET_ATTR",
    "LOAD_MODES_ATTR",
    "load_app_cross_unit_charts",
    "load_app_default_preset",
    "load_app_load_modes",
    "load_app_launch_probe",
    "load_app_presets",
    "load_app_round_prepare",
    "load_app_round_prepare_before_retry",
    "load_app_round_teardown",
    "load_app_run_setup",
    "load_app_scenarios",
    "load_app_startup_ready_js",
    "load_app_unit_sequence",
    "load_app_warmup_default",
]

#: app 侧 perf 场景包的模块路径模板（``{app}`` 为 app 名）
DEFAULT_SCENARIOS_MODULE_TEMPLATE = "apps.{app}.perf.scenarios"

#: app 侧性能预设模块（``PRESETS`` 字典，`--perf-preset` 用；也承载 ``UNIT_SEQUENCE``）
DEFAULT_PRESETS_MODULE_TEMPLATE = "apps.{app}.perf.presets"

#: app 侧「默认性能预设」常量名（``DEFAULT_PRESET = "xxx"``）：命令行未传 ``--perf-preset``
#: 且 app 声明了它时自动选中，用一条命令跑通（无需用户记预设名）。
DEFAULT_PRESET_ATTR = "DEFAULT_PRESET"

#: app 侧「启动就绪探针」模块（``READY_JS``：判定"窗口出现且左上角品牌已渲染"的自包含 JS）
DEFAULT_STARTUP_READY_MODULE_TEMPLATE = "apps.{app}.perf.startup_ready"

#: app 侧「多单元编排序列」常量名（见 :func:`load_app_unit_sequence`）
UNIT_SEQUENCE_ATTR = "UNIT_SEQUENCE"

#: app 侧「perf 默认值」常量名（dict，见 :func:`load_app_warmup_default`；目前支持键 ``warmup``）
APP_DEFAULTS_ATTR = "APP_DEFAULTS"

#: app 侧「可遍历取值清单」常量名（tuple，见 :func:`load_app_load_modes`）
LOAD_MODES_ATTR = "LOAD_MODES"

#: app 侧「跨单元对比图」常量名（list，见 :func:`load_app_cross_unit_charts`）
CROSS_UNIT_CHARTS_ATTR = "CROSS_UNIT_CHARTS"

#: app 侧「每轮前置准备」模块（``prepare(ctx)``：杀进程等每次启动前的动作）
DEFAULT_ROUND_PREPARE_MODULE_TEMPLATE = "apps.{app}.perf.round_prepare"

#: app 侧「每轮收尾」模块（``teardown(ctx)``：场景测量之后的动作，典型是合法退出应用）
DEFAULT_ROUND_TEARDOWN_MODULE_TEMPLATE = "apps.{app}.perf.round_teardown"

#: app 侧「批次级环境准备」模块（``setup(ctx)``：整批只跑一次、且必须早于任何启动的动作）
DEFAULT_RUN_SETUP_MODULE_TEMPLATE = "apps.{app}.perf.run_setup"

#: app 侧「启动失败重试前也要过一遍每轮准备」开关的常量名（包 ``apps.<app>.perf`` 模块级 bool）。
#: 见 :func:`load_app_round_prepare_before_retry`。
ROUND_PREPARE_BEFORE_RETRY_ATTR = "PERF_ROUND_PREPARE_BEFORE_RETRY"

#: app 侧 perf 包模块路径模板（承载上面这个开关的包 ``__init__``）
DEFAULT_PERF_PACKAGE_TEMPLATE = "apps.{app}.perf"

#: app 侧「启动就绪探针」模块（``create_probe(app_name, os_name, ...)`` 或 ``PROBE``；
#: 用于原生引擎——无 CDP/JS 能力时替代 ``AppProcessProbe``）
DEFAULT_LAUNCH_PROBE_MODULE_TEMPLATE = "apps.{app}.perf.launch_probe"


def load_app_presets(
    app_name: str, *, module_template: Optional[str] = None,
) -> dict:
    """按约定动态导入 app 的预设模块，返回 ``PRESETS`` 字典（无则返回 ``{}``）。

    与场景包同款策略：app 没有预设是常态（跳过）；但预设模块内部的 import 错误会原样抛出。
    """
    app = (app_name or "").strip()
    if not app:
        return {}
    module_path = (module_template or DEFAULT_PRESETS_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf] app=%s 未提供预设模块 %s（跳过）", app, module_path)
            return {}
        raise
    presets = getattr(module, "PRESETS", None) or {}
    if not isinstance(presets, dict):
        raise ValueError(f"{module_path}.PRESETS 必须是 dict，实际是 {type(presets).__name__}")
    return presets


def load_app_default_preset(
    app_name: str, *, module_template: Optional[str] = None,
) -> str:
    """读取 app 声明的默认性能预设名（``PRESETS`` 模块里的 ``DEFAULT_PRESET``）。

    Returns:
        预设名；app 未声明/未提供预设模块时返回 ``""``（调用方回落既有行为）。

    Note:
        声明的名字**必须**在自己的 ``PRESETS`` 里存在——不存在由调用方报错，
        本函数不做二次校验（避免两处判断口径不一致）。
    """
    app = (app_name or "").strip()
    if not app:
        return ""
    module_path = (module_template or DEFAULT_PRESETS_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        return ""
    value = getattr(module, DEFAULT_PRESET_ATTR, "") or ""
    return str(value).strip()


def load_app_warmup_default(
    app_name: str, *, module_template: Optional[str] = None,
) -> Optional[bool]:
    """读取 app 声明的「默认是否跑热身轮」（``apps.<app>.perf.presets`` 的 ``APP_DEFAULTS["warmup"]``）。

    为什么放在 app 侧：热身轮是否值得跑取决于**这个 app 的测量口径**——例如"每次启动都以
    磁盘缓存关闭状态冷启动"的应用（``--use-cache false``）每轮本来就是冷启动，再拿一轮去
    "吸收首次启动的一次性开销"没有意义，反而多跑一轮冷启动+多清一次环境。这是应用知识（守 R2）。

    Returns:
        ``True`` / ``False``（app 显式声明布尔值）；未声明、未提供模块、值不是布尔时返回 ``None``
        ——调用方据此回落 ``config.PERF_WARMUP_ENABLED``（既有行为，零回归）。
    """
    app = (app_name or "").strip()
    if not app:
        return None
    module_path = (module_template or DEFAULT_PRESETS_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        return None
    defaults = getattr(module, APP_DEFAULTS_ATTR, None)
    if not isinstance(defaults, dict):
        return None
    value = defaults.get("warmup")
    if isinstance(value, bool):
        logger.info("[perf] app=%s 声明默认热身轮 warmup=%s", app, value)
        return value
    if value is not None:
        logger.warning(
            "[perf] %s.%s['warmup'] 不是布尔值（实际 %s），已忽略（回落全局默认）",
            module_path, APP_DEFAULTS_ATTR, type(value).__name__,
        )
    return None


def load_app_load_modes(
    app_name: str, *, module_template: Optional[str] = None,
) -> tuple[str, ...]:
    """读取 app 声明「可遍历的取值清单」（``apps.<app>.perf.presets`` 的 ``LOAD_MODES``）。

    用途：``--load-mode all``（默认）时按这个顺序逐个跑单元并聚合对比——"有哪些取值、
    按什么顺序跑"是应用知识（守 R2）。

    Returns:
        有序取值元组（去重、保序、忽略非字符串/空串）；未声明/未提供模块时返回 ``()``
        ——调用方据此认为该 app 没有"按取值遍历"这回事（单次运行，零回归）。
    """
    app = (app_name or "").strip()
    if not app:
        return ()
    module_path = (module_template or DEFAULT_PRESETS_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        return ()
    raw = getattr(module, LOAD_MODES_ATTR, None)
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        logger.warning(
            "[perf] %s.%s 不是 list/tuple（实际 %s），已忽略",
            module_path, LOAD_MODES_ATTR, type(raw).__name__,
        )
        return ()
    modes: list[str] = []
    for item in raw:
        text = str(item or "").strip().lower()
        if text and text not in modes:
            modes.append(text)
    return tuple(modes)


def load_app_cross_unit_charts(
    app_name: str, *, module_template: Optional[str] = None,
) -> list[dict[str, Any]]:
    """读取 app 声明「多单元聚合报告的跨单元对比图」（``CROSS_UNIT_CHARTS``）。

    结构：``[{"title": str, "unit": str, "series": [{"key": aggregate 扁平字段, "label": str}]}]``。
    "哪几项指标该放一张图、单位是什么"是应用知识；core 只按声明取 ``aggregate[key].avg`` 画图。
    未声明/格式不符时返回 ``[]``（报告不出该区块，零回归）。
    """
    app = (app_name or "").strip()
    if not app:
        return []
    module_path = (module_template or DEFAULT_PRESETS_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        return []
    raw = getattr(module, CROSS_UNIT_CHARTS_ATTR, None)
    if not isinstance(raw, (list, tuple)):
        return []
    groups: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        series = [x for x in (item.get("series") or []) if isinstance(x, dict) and x.get("key")]
        if not series:
            continue
        groups.append({
            "title": str(item.get("title") or ""),
            "unit": str(item.get("unit") or ""),
            "series": [
                {"key": str(x["key"]), "label": str(x.get("label") or x["key"])} for x in series
            ],
        })
    return groups


def load_app_scenarios(
    app_name: str, *, module_template: Optional[str] = None,
) -> list[str]:
    """导入 app 的场景包（导入即注册），返回本次**新增**的场景名。

    Args:
        app_name: app 名（如 ``sample_media``）
        module_template: 模块路径模板，默认 ``apps.{app}.perf.scenarios``

    Returns:
        新增注册的场景名（已注册的不重复计入）；app 未提供场景包时返回 ``[]``。
    """
    app = (app_name or "").strip()
    if not app:
        return []
    module_path = (module_template or DEFAULT_SCENARIOS_MODULE_TEMPLATE).format(app=app)
    before = set(available_scenarios())
    try:
        importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        # 目标包（或其父包）本身不存在 → 该 app 未提供场景，属常态
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf] app=%s 未提供场景包 %s（跳过）", app, module_path)
            return []
        raise  # 包内子模块缺失 → 配置/代码错误，必须暴露
    added = sorted(set(available_scenarios()) - before)
    if added:
        logger.info("[perf] app=%s 已注册场景: %s", app, ", ".join(added))
    else:
        logger.debug("[perf] app=%s 场景包 %s 未注册任何场景", app, module_path)
    return added


def load_app_startup_ready_js(
    app_name: str, *, module_template: Optional[str] = None,
) -> str:
    """载入 app 侧「启动就绪探针 JS」（模块常量 ``READY_JS``），无则返回空串。

    为什么放在 app 侧：判定"窗口左上角是否渲染出应用品牌"要用**选择器 + 品牌文案 +
    左上角位置**，都是应用知识（守 R2：``core/`` 不得含 ``apps`` 知识）。core 只把这段 JS
    交给 ``AppProcessProbe(ready_js=...)`` 求值。

    Returns:
        就绪 JS 字符串；app 未提供该模块（常态）/ ``READY_JS`` 不是非空字符串时返回 ``""``
        ——调用方据此退回通用口径（``document.readyState === 'complete'``），零回归。
    """
    app = (app_name or "").strip()
    if not app:
        return ""
    module_path = (module_template or DEFAULT_STARTUP_READY_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf] app=%s 未提供启动就绪探针模块 %s（退回 readyState 口径）",
                         app, module_path)
            return ""
        raise
    js = getattr(module, "READY_JS", "")
    if not isinstance(js, str) or not js.strip():
        logger.warning("[perf] %s.READY_JS 不是非空字符串，已忽略（退回 readyState 口径）",
                       module_path)
        return ""
    logger.info("[perf] app=%s 启动就绪探针：%s（%d 字符 JS）", app, module_path, len(js))
    return js


def load_app_unit_sequence(
    app_name: str, *, module_template: Optional[str] = None,
) -> list[dict[str, Any]]:
    """读 app 侧「多单元编排序列」（``UNIT_SEQUENCE``），返回归一化后的单元声明列表。

    **为什么放在 app 侧**：单元里会出现**应用自己的场景名**（如 ``only=["stress"]``）与
    "每个单元用哪份库规格/哪个预设"的编排知识——``stress`` 是 sample_media 的场景，属应用知识
    （守 R2：``core/`` 不得认识某 app 的场景名）。core 只认五个通用键：

      - ``name``（单元名 → 产物 ``tier-<name>/``、报告列名）；
      - ``lib_tier``（样本库规格，决定目标份数与 ``lib-<tier>`` 档位；缺省 = ``name``）；
      - ``tag``（该单元在产物/CSV 里的**档位标签**；缺省 ``""`` = 按 ``lib_tier`` 推成
        ``lib-<tier>``。仅当单元的库规格与身份不一致时才要显式给——如压测单元库用 10k、
        身份叫 ``stress``，若沿用 ``lib-10k`` 会与 10k 单元在合并 CSV 的 ``tier`` 列撞名）；
      - ``preset``（该单元用哪个预设展开场景；缺省 ``""`` = 由 CLI/默认逻辑决定）；
      - ``only`` / ``exclude``（场景清单白/黑名单，**复用该预设的选项**，不必为每个单元
        复制一份场景清单与选项）。

    声明形如::

        UNIT_SEQUENCE = [
            {"name": "0k",     "lib_tier": "0k",  "preset": "full", "exclude": ["stress"]},
            {"name": "5k",     "lib_tier": "5k",  "preset": "full", "exclude": ["stress"]},
            {"name": "10k",    "lib_tier": "10k", "preset": "full", "exclude": ["stress"]},
            {"name": "50k",    "lib_tier": "50k", "preset": "full", "exclude": ["stress"]},
            {"name": "stress", "lib_tier": "10k", "tag": "stress", "preset": "full",
             "only": ["stress"]},
        ]

    Returns:
        归一化后的单元列表（每项含 ``name`` / ``lib_tier`` / ``tag`` / ``preset`` /
        ``only`` / ``exclude``）。app 未声明该常量时返回 ``[]``（**常态**）——调用方据此
        回落到 core 的默认规模档位序列（``0k/5k/10k/50k``），既有 app 行为零回归。

    Raises:
        ValueError: 声明格式非法（非列表 / 项非 dict / ``name`` 缺失或重复 /
            ``only`` 与 ``exclude`` 并存 / ``only``/``exclude`` 类型不对）。
            **配置错误必须暴露**：静默降级成默认 4 档会让"以为跑了 5 个单元、实际少跑一个"
            完全不可见（真机代价是几小时的运行 + 一份看不出缺项的产物）。
    """
    app = (app_name or "").strip()
    if not app:
        return []
    module_path = (module_template or DEFAULT_PRESETS_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf] app=%s 未提供预设模块 %s（无多单元序列）", app, module_path)
            return []
        raise
    raw = getattr(module, UNIT_SEQUENCE_ATTR, None)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"{module_path}.{UNIT_SEQUENCE_ATTR} 必须是列表，实际是 {type(raw).__name__}",
        )
    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"{module_path}.{UNIT_SEQUENCE_ATTR}[{index}] 必须是 dict，"
                f"实际是 {type(item).__name__}",
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"{module_path}.{UNIT_SEQUENCE_ATTR}[{index}] 缺少 name")
        if name in seen:
            raise ValueError(f"{module_path}.{UNIT_SEQUENCE_ATTR} 单元名重复: {name!r}")
        seen.add(name)
        only = coerce_name_filter(item.get("only"), f"{UNIT_SEQUENCE_ATTR}.only")
        exclude = coerce_name_filter(item.get("exclude"), f"{UNIT_SEQUENCE_ATTR}.exclude")
        if only is not None and exclude is not None:
            raise ValueError(
                f"{module_path}.{UNIT_SEQUENCE_ATTR}[{index}]（{name}）"
                "only 与 exclude 不能同时声明",
            )
        units.append({
            "name": name,
            "lib_tier": str(item.get("lib_tier") or name).strip(),
            # 档位标签：缺省空串 = 由调用方按 lib_tier 推成 lib-<tier>（既有产物口径不变）
            "tag": str(item.get("tag") or "").strip(),
            "preset": str(item.get("preset") or "").strip(),
            "only": only,
            "exclude": exclude,
        })
    logger.info(
        "[perf] app=%s 多单元编排序列: %s", app, " → ".join(u["name"] for u in units),
    )
    return units


def load_app_run_setup(
    app_name: str, *, module_template: Optional[str] = None,
) -> Optional[Any]:
    """载入 app 侧「**批次级**环境准备」函数（``setup(ctx)``），未提供时返回 ``None``。

    与 :func:`load_app_round_prepare` 的分工（都是"启动前动作"，但作用域不同）：

    | 扩展点 | 调用次数 | 典型用途 |
    |---|---|---|
    | ``run_setup.setup(ctx)`` | 整批**一次** | 写被测客户端的**启动期配置**（客户端只在启动时读一次），如页面预热模式 / 磁盘缓存开关 |
    | ``round_prepare.prepare(ctx)`` | 每轮**一次** | 结束应用、复位运行态（必须在每次 launch 之前） |

    core 只认"有个可调用对象、返回 dict"；写哪个文件、有哪些键全是应用知识（守 R2）。
    该函数可以是同步或异步（runner 侧统一 await 处理），入参只有一份 ``ctx`` dict。
    """
    app = (app_name or "").strip()
    if not app:
        return None
    module_path = (module_template or DEFAULT_RUN_SETUP_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf] app=%s 未提供批次环境准备模块 %s（跳过）", app, module_path)
            return None
        raise
    setup = getattr(module, "setup", None)
    if setup is None:
        logger.debug("[perf] %s 未定义 setup(ctx)，跳过批次环境准备", module_path)
        return None
    if not callable(setup):
        raise TypeError(f"{module_path}.setup 必须是可调用对象，实际是 {type(setup).__name__}")
    return setup


def load_app_round_prepare(
    app_name: str, *, module_template: Optional[str] = None,
) -> Optional[Any]:
    """载入 app 侧「每轮前置准备」函数（``prepare(ctx)``），未提供时返回 ``None``。

    为什么需要（性能场景口径）：有的被测场景要求**每一轮启动前**先把环境复位
    ——典型如"杀进程 + 删缓存目录后再冷启动"（缓存命中与否直接决定启动耗时是否可比）。
    这类动作必须在 ``prepare_engine_for_case()`` **之前**发生，塞进 scenario（启动后才测量）
    语义正好相反，故单独开一个扩展点。

    分层（守 R2）：杀哪个进程、删哪个目录、要不要提权全是**应用知识**，住
    ``apps/<app>/perf/round_prepare.py``；core 只认「有个可调用对象、返回 dict」。
    该函数可以是同步或异步（runner 侧统一 await 处理），入参只有一份 ``ctx`` dict。

    Returns:
        ``prepare`` 可调用对象；app 未提供该模块（**常态**）/ 模块里没有 ``prepare``
        时返回 ``None``（调用方据此跳过，零回归）。

    Raises:
        ModuleNotFoundError: 包内子模块缺失（模块自身不存在时按"未提供"跳过）。
        TypeError: ``prepare`` 存在但不是可调用对象（配置错误必须暴露）。
    """
    app = (app_name or "").strip()
    if not app:
        return None
    module_path = (module_template or DEFAULT_ROUND_PREPARE_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf] app=%s 未提供每轮前置准备模块 %s（跳过）", app, module_path)
            return None
        raise
    prepare = getattr(module, "prepare", None)
    if prepare is None:
        logger.debug("[perf] %s 未定义 prepare(ctx)，跳过每轮前置准备", module_path)
        return None
    if not callable(prepare):
        raise TypeError(f"{module_path}.prepare 必须是可调用对象，实际是 {type(prepare).__name__}")
    logger.info("[perf] app=%s 每轮前置准备：%s", app, module_path)
    return prepare


def load_app_round_teardown(
    app_name: str, *, module_template: Optional[str] = None,
) -> Optional[Any]:
    """载入 app 侧「每轮收尾」函数（``teardown(ctx)``），未提供时返回 ``None``。

    与 :func:`load_app_round_prepare` 对称：那个管"启动之前"，这个管"场景测完之后"。
    典型用途是**合法退出被测应用**（如 sample_web 的托盘 ``quit_app``），而不是把它挂到下一轮闸门
    才退出——一轮测完还占着 CPU/内存，会让"本轮测量态"与"轮间隔的机器态"混在一起。

    core 只认"有个可调用对象、返回 dict"；退出哪个进程、怎么退出全是应用知识（守 R2）。

    Returns:
        ``teardown`` 可调用对象；app 未提供该模块（**常态**）/ 模块里没有 ``teardown``
        时返回 ``None``（调用方跳过，``rounds[].teardown`` 保持空 dict，零回归）。

    Raises:
        ModuleNotFoundError: 包内子模块缺失（模块自身不存在时按"未提供"跳过）。
        TypeError: ``teardown`` 存在但不是可调用对象（配置错误必须暴露）。
    """
    app = (app_name or "").strip()
    if not app:
        return None
    module_path = (module_template or DEFAULT_ROUND_TEARDOWN_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf] app=%s 未提供每轮收尾模块 %s（跳过）", app, module_path)
            return None
        raise
    teardown = getattr(module, "teardown", None)
    if teardown is None:
        logger.debug("[perf] %s 未定义 teardown(ctx)，跳过每轮收尾", module_path)
        return None
    if not callable(teardown):
        raise TypeError(f"{module_path}.teardown 必须是可调用对象，实际是 {type(teardown).__name__}")
    logger.info("[perf] app=%s 每轮收尾：%s", app, module_path)
    return teardown


def load_app_round_prepare_before_retry(
    app_name: str, *, module_template: Optional[str] = None,
) -> bool:
    """app 是否声明「启动失败重试之前也再跑一遍每轮前置准备」（默认 ``False``）。

    背景：``round_prepare.prepare(ctx)`` 每轮只在**首发**启动前跑一次；重试（``PERF_LAUNCH_ATTEMPTS``
    的第 2/3 次）走 ``force_hard_restart`` + 框架的杀进程清场（``AppProcess.kill_all``），**不经过**
    app 自己的退出路径。对有单实例锁 / 驱动级自我保护的应用（如 sample_web：``DemoTray`` 吞掉新实例、
    外部强杀可能被静默放行），重试就可能继续撞"旧实例还在"，白烧一次启动超时。

    声明方式：``apps/<app>/perf/__init__.py`` 模块级常量 ``PERF_ROUND_PREPARE_BEFORE_RETRY = True``。
    读不到常量 / 包不存在 / 导入异常 → ``False``（默认行为：每轮只准备一次，零回归）。

    Returns:
        ``True`` = 重试前复用同一 ``prepare(ctx)``；若其返回 ``abort_launch=True``，本轮直接判启动失败
        （不再消耗剩余重试）。``False`` = 框架默认（不额外准备）。
    """
    app = (app_name or "").strip()
    if not app:
        return False
    module_path = (module_template or DEFAULT_PERF_PACKAGE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
        value = getattr(module, ROUND_PREPARE_BEFORE_RETRY_ATTR, False)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf] app=%s 无 %s，重试前不做额外准备", app, module_path)
            return False
        raise
    except Exception as exc:  # noqa: BLE001 - 开关读不到只影响"重试前准备"，不该挡住 perf
        logger.warning("[perf] app=%s 读取 %s 失败（按 False 处理）: %s", app, module_path, exc)
        return False
    enabled = bool(value)
    if enabled:
        logger.info("[perf] app=%s 启动失败重试前也执行每轮准备（%s）", app, module_path)
    return enabled


def load_app_launch_probe(
    app_name: str, *, module_template: Optional[str] = None,
) -> Optional[Any]:
    """载入 app 侧「启动就绪探针」工厂（原生引擎替代 ``AppProcessProbe``），无则 ``None``。

    为什么需要：``LaunchTimer`` 的默认探针（``AppProcessProbe``）走
    ``engine.app_process`` + CDP 端口 + JS 求值，只对 Electron 类应用成立；
    原生应用（如 WinUIA / MacAppKit 引擎）没有这些成员 → ``startup_ms`` 恒为
    ``None``，只剩"调用 prepare 的墙钟"。app 侧可提供自己的探针（进程名 + 窗口类名等
    **应用知识**），core 只认下面的鸭子类型契约：

      - ``available``：bool，探针是否可用（``False`` → 不做分段计时，只留 ``launch_ms``）；
      - ``current_pid() -> int|None``：当前主进程 pid（用于"旧进程消失 → 新进程出现"）；
      - ``ready() -> bool``：应用是否已可交互（替代 CDP 就绪；**优先于** ``cdp_ready``）；
      - ``render_ready() -> bool|None``（可选）：更严格的就绪判定（窗口已渲染）；
      - ``render_probe``（可选）：口径名（落产物，供自证"按什么判定的"）。

    工厂签名：``create_probe(app_name=<app>) -> 探针对象|None``（平台差异由 app 侧读
    ``sys.platform`` 自行判定，core 不传平台）；也接受模块直接暴露已构造好的 ``PROBE`` 对象。

    Returns:
        探针对象；app 未提供该模块（**常态**）/ 既无 ``create_probe`` 也无 ``PROBE``
        时返回 ``None``（调用方回退默认探针，零回归）。
    """
    app = (app_name or "").strip()
    if not app:
        return None
    module_path = (module_template or DEFAULT_LAUNCH_PROBE_MODULE_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf] app=%s 未提供启动就绪探针模块 %s（用默认探针）", app, module_path)
            return None
        raise
    factory = getattr(module, "create_probe", None)
    if callable(factory):
        probe = factory(app_name=app)
        if probe is not None:
            logger.info(
                "[perf] app=%s 启动就绪探针：%s（render_probe=%s）",
                app, module_path, getattr(probe, "render_probe", ""),
            )
        return probe
    probe = getattr(module, "PROBE", None)
    if probe is not None:
        logger.info("[perf] app=%s 启动就绪探针：%s.PROBE", app, module_path)
    return probe
