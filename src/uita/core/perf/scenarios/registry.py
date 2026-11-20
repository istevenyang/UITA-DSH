"""perf 场景注册表（核心只提供**机制**，具体场景由各 app 注册）。

分层约定（宪法 R2）：``core/`` 不得静态 import ``apps.xxx``。
场景里的选择器/URL/注入 API 都是**应用知识**（如 ``.tl-photo``、``media-viewer``、
``localScannerAPI``），因此它们住在 ``apps/<app>/perf/scenarios/``，通过
:func:`register_scenario` 或导入即注册的方式挂进本注册表；
core 只认识"模块需导出 ``measure(bridge, **opts)``"这一契约。

场景模块契约（鸭子类型，不要求继承）：
  - ``measure(bridge, **opts) -> dict``（必需）：单轮测量，自包含导航/前置准备；
  - ``to_aggregate_fields(result) -> dict``（可选）：映射为基线判定用的扁平字段；
  - ``AGGREGATE_PICK: dict[str, str]``（可选）：字段级跨轮聚合方向（``min``/``max``）；
  - ``REPORT_KEYS: dict[str, str]``（可选）：报告前台展示的关键指标（``{字段名: 中文标签}``，有序）；
  - ``REPORT_DETAIL: dict[str, Any]``（可选）：报告前台展示的"逐级/逐项明细表"（见
    :func:`report_detail`），如 ``stress`` 场景的每级注入量/启动耗时；
  - ``SCENARIO_LABEL: str``（可选）：报告里的中文场景名；
  - ``DEFAULT_OPTIONS: dict``（可选）：场景选项默认值；
  - ``SCENARIO_NAME: str``（可选）：默认用模块名。
引擎能力不足时抛 ``ScenarioUnsupported``（记 ``unsupported``，不伪造 0）。
"""
from __future__ import annotations

import inspect
import logging
from functools import lru_cache
from typing import Any, Optional

logger = logging.getLogger("uita")

__all__ = [
    "SCENARIOS",
    "accepted_options",
    "aggregate_fields",
    "aggregate_pick",
    "as_list",
    "available_scenarios",
    "default_options",
    "register_scenario",
    "report_detail",
    "report_keys",
    "run_scenario",
    "scenario_label",
]

#: 场景名 → 模块（应用侧导入即注册）
SCENARIOS: dict[str, Any] = {}


def register_scenario(module: Any, *, name: Optional[str] = None, overwrite: bool = False) -> str:
    """注册一个场景模块，返回其场景名。

    重名默认**报错**而非静默覆盖：多 app 共存时静默覆盖会让"量的是谁"变得不可追溯。
    """
    scenario_name = str(name or getattr(module, "SCENARIO_NAME", "") or getattr(module, "__name__", "")).strip()
    if not scenario_name:
        raise ValueError("场景名不能为空（需提供 name 或模块 SCENARIO_NAME）")
    if not callable(getattr(module, "measure", None)):
        raise ValueError(f"场景 {scenario_name!r} 缺少 measure(bridge, **opts) 契约")
    if scenario_name in SCENARIOS and not overwrite:
        raise ValueError(f"场景 {scenario_name!r} 已注册（如需替换请传 overwrite=True）")
    SCENARIOS[scenario_name] = module
    return scenario_name


def available_scenarios() -> list[str]:
    return sorted(SCENARIOS)


def as_list(value: Any, default: list[Any]) -> list[Any]:
    """把 CLI 传入的标量（``--perf-scenario-opt keywords=a|b``）安全转成列表。

    必须显式处理 ``str``：``list("sim_1")`` 会被拆成字符 `['s','i','m',...]`（实测踩过）。
    """
    if value is None or value == "":
        return list(default)
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("，", ",").replace("|", ",").split(",")]
        return [p for p in parts if p] or list(default)
    if isinstance(value, (list, tuple)):
        return [v for v in value]
    return [value]


def default_options(name: str) -> dict[str, Any]:
    module = SCENARIOS.get(name)
    return dict(getattr(module, "DEFAULT_OPTIONS", {}) or {}) if module else {}


@lru_cache(maxsize=None)
def accepted_options(name: str) -> frozenset[str]:
    """该场景 ``measure()`` 接受的选项名（按签名推导）。

    必须过滤的原因（真机实测）：``--perf-scenario-opt`` 是一份**全局**字典，
    若原样透传给每个场景，``path=``／``dir=`` 这类只属于某场景的键会触发
    ``TypeError: measure() got an unexpected keyword argument``，导致全部场景直接失败。
    """
    module = SCENARIOS.get(name)
    if module is None:
        return frozenset()
    params = inspect.signature(module.measure).parameters
    return frozenset(
        pname for pname, param in params.items()
        if pname != "bridge" and param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    )


async def run_scenario(name: str, bridge: Any, **opts: Any) -> dict[str, Any]:
    """执行指定场景的一次测量（未知场景抛 ``ValueError``）。

    选项按场景签名过滤（见 ``accepted_options``）+ 场景默认值兜底。
    """
    module = SCENARIOS.get(name)
    if module is None:
        raise ValueError(f"未知场景 {name!r}（可选: {', '.join(available_scenarios())}）")
    allowed = accepted_options(name)
    merged = default_options(name)
    merged.update({k: v for k, v in opts.items() if v is not None and k in allowed})
    return await module.measure(bridge, **merged)


def aggregate_fields(name: str, result: dict[str, Any]) -> dict[str, Optional[float]]:
    """把场景单轮结果映射为聚合字段（无映射函数的场景返回空 dict）。"""
    module = SCENARIOS.get(name)
    fn = getattr(module, "to_aggregate_fields", None)
    if module is None or not callable(fn):
        return {}
    try:
        return fn(result)
    except Exception as exc:  # noqa: BLE001 - 映射失败不影响主流程
        logger.debug("[perf] 场景 %s 聚合映射失败: %s", name, exc)
        return {}


def aggregate_pick(name: str) -> dict[str, str]:
    """字段级跨轮聚合方向（``min``/``max``）；缺省为 ``max``。"""
    module = SCENARIOS.get(name)
    return dict(getattr(module, "AGGREGATE_PICK", {}) or {}) if module else {}


def report_keys(name: str) -> dict[str, str]:
    """该场景在报告**前台**展示的关键指标（有序 ``{聚合字段名: 中文标签}``）。

    2026-10-13 新增（用户口径："整那么多细碎的耗时干啥？"）：报告前台只展示白名单字段，
    其余字段仍完整保留在 ``perf_summary.json`` / ``perf_metrics.csv``（HTML 不再铺开）。

    这是**应用知识**（哪个指标对用户有意义、中文叫什么），故住 app 侧场景模块，core 只认机制：
    未声明 ``REPORT_KEYS`` 的场景返回空 dict → 前台给一行"未声明关键指标"提示（**不静默丢数据**）。
    """
    module = SCENARIOS.get(name)
    keys = getattr(module, "REPORT_KEYS", None) if module is not None else None
    if not isinstance(keys, dict) or not keys:
        return {}
    return {str(k): str(v) for k, v in keys.items()}


def scenario_label(name: str) -> str:
    """场景的**中文名**（报告前台用）；未声明 ``SCENARIO_LABEL`` 时回退场景名本身。"""
    module = SCENARIOS.get(name)
    label = str(getattr(module, "SCENARIO_LABEL", "") or "") if module is not None else ""
    return label or name


def report_detail(name: str) -> dict[str, Any]:
    """该场景在报告**前台**展示的"逐级/逐项明细表"契约（可选，2026-10-14 六轮加固）。

    用户口径（stress 场景"每级启动耗时 start_to_ready_ms"）：总体聚合（均值/最小/最大）之外，
    还要看"按级逐条排列"的明细——这类数据住在单轮 ``measure()`` 返回 dict 里的某个 ``list[dict]``
    字段（如 stress 的 ``steps``），core 不认识该字段名/含义，只认机制：

    模块声明
    ``REPORT_DETAIL = {"source": "<list 字段名>", "key": "<行标识字段名，可选>", "fields": {"<每项里的字段名>": "中文表头"}}``，
    ``fields`` 为有序 dict（展示列顺序）。未声明或声明不完整（缺 ``source``/空 ``fields``）返回空 dict
    → 报告不渲染该场景的明细表（不是必需契约，只有需要的场景才声明）。

    ``key``（2026-10-14 八轮新增，可选）：该场景若跑多轮（``--perf-repeat N``），报告按
    ``key`` 字段把"第几级/第几项"跨轮对齐后再聚合（数值字段给均值/最小/最大，分类字段给
    去重计数），见 ``report.py::_detail_rows``；未声明时按列表位置对齐（要求各轮该 list 的
    顺序语义一致，如均按"第几级"顺序排列——stress 恰好满足，故仍声明 ``key="step"`` 更稳妥，
    不依赖"各轮级数相同"这一隐含假设）。
    """
    module = SCENARIOS.get(name)
    detail = getattr(module, "REPORT_DETAIL", None) if module is not None else None
    if not isinstance(detail, dict):
        return {}
    source = str(detail.get("source") or "").strip()
    fields = detail.get("fields")
    if not source or not isinstance(fields, dict) or not fields:
        return {}
    out: dict[str, Any] = {"source": source, "fields": {str(k): str(v) for k, v in fields.items()}}
    key = detail.get("key")
    if key:
        out["key"] = str(key)
    return out
