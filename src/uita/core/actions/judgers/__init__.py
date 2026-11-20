"""
core/actions/judgers — assert/verify 判定器体系（P2 新增）。

对外主入口：
  select_judgers(spec) -> list[JudgerBase]   按 kwargs 派发判定器（AND 组合）
  needs_element(kwargs) -> bool              该 kwargs 组合是否需要先 resolve 元素

见 docs/plans/archive/02-dsl-actions-实施纪要.md。
"""
from __future__ import annotations

from .absence import AbsenceJudger
from .attribute import AttributeJudger
from .base import JudgeResult, JudgerBase, JudgerSpec
from .cls import ClassJudger
from .content import ContentJudger
from .count import CountJudger
from .existence import ExistenceJudger
from .js import JsJudger
from .numeric import NumericJudger
from .path_exists import PathExistsJudger, is_path_like
from .prompt import PromptJudger
from .selector import SelectorJudger
from .title import TitleJudger
from .url import UrlJudger
from .var_expr import VarExprJudger, is_var_expr


class JudgerDispatchError(Exception):
    """kwarg 组合非法（如 absent 与其他 kwarg 同时声明）或包含未知 kwarg。"""


# 判定不消费的保留 kwarg（由 action_assert.py 自身消费）
_RESERVED_KEYS = {"timeout"}

# 判定器行为修饰 kwarg：不单独选择判定器，而是调整既有判定器的行为，由对应
# 判定器自身读取。visible=（默认 true）由 ExistenceJudger 消费——visible=false
# 时仅断言元素存在、跳过可见性校验（见 existence.py 的 _want_visible）。
# 单独声明 visible= 时不改变判定器选择（仍走默认 ExistenceJudger），故需从
# select_judgers/needs_element 的 keys 统计中排除，避免被当作未知 kwarg 报错。
_MODIFIER_KEYS = {"visible"}

_CONTENT_KEYS = {"contains", "not_contains"}
_COUNT_KEYS = {"count_min", "count_max", "count_eq", "count_gt", "count_lt"}
_ATTRIBUTE_KEYS = {"disabled", "checked", "value"}
_CLASS_KEYS = {"has_class", "not_has_class"}
_JS_KEYS = {"js"}
_URL_KEYS = {"url", "url_contains", "url_regex"}
_TITLE_KEYS = {"title", "title_contains"}
_PROMPT_KEYS = {"prompt"}
_NUMERIC_KEYS = {"lt", "le", "gt", "ge", "between"}
# selector= 直查自带定位能力（不依赖 description 先 resolve），见
# docs/plans/archive/02-dsl-actions-实施纪要.md §3.7 风险 5
_SELECTOR_KEYS = {"selector"}

# "无元素类"判定：description 可省略，assert 无需先 resolve 元素；数值类目标必填。
_NO_ELEMENT_KEYS = _JS_KEYS | _URL_KEYS | _TITLE_KEYS | _PROMPT_KEYS | _SELECTOR_KEYS | _NUMERIC_KEYS


def _is_falsy(value: str) -> bool:
    return value.strip().lower() in ("false", "0", "", "no")


def _match_judger_cls(key: str):
    if key in _CONTENT_KEYS:
        return ContentJudger
    if key in _COUNT_KEYS:
        return CountJudger
    if key in _ATTRIBUTE_KEYS or key.startswith("attr_"):
        return AttributeJudger
    if key in _CLASS_KEYS:
        return ClassJudger
    if key in _JS_KEYS:
        return JsJudger
    if key in _URL_KEYS:
        return UrlJudger
    if key in _TITLE_KEYS:
        return TitleJudger
    if key in _PROMPT_KEYS:
        return PromptJudger
    if key in _SELECTOR_KEYS:
        return SelectorJudger
    if key in _NUMERIC_KEYS:
        return NumericJudger
    return None


def _validate_numeric_keys(keys: list[str]) -> None:
    if not any(key in _NUMERIC_KEYS for key in keys):
        return
    unknown = [key for key in keys if key != "absent" and _match_judger_cls(key) is None]
    if unknown:
        raise JudgerDispatchError(f"未知的断言 kwarg: {unknown}")
    mixed = [key for key in keys if key not in _NUMERIC_KEYS]
    if mixed:
        raise JudgerDispatchError(f"数值判定与其他判定 kwarg 互斥: {mixed}")


def select_judgers(spec: JudgerSpec) -> list[JudgerBase]:
    """按 spec.kwargs 派发到对应判定器列表（poll 循环里 AND 组合）。

    - 无任何有效 kwarg → 按顺序检查：裸括号内容命中路径特征（见
      ``is_path_like()``）时 ``[PathExistsJudger()]``（2026-07-25 新增，见
      docs/plans/archive/02-dsl-actions-实施纪要.md）；命中变量比较/判空
      函数调用表达式（见 ``is_var_expr()``）时 ``[VarExprJudger()]``
      （2026-07-27 新增，见
      docs/plans/archive/02-dsl-actions-实施纪要.md）；否则
      ``[ExistenceJudger()]``（对齐现状默认行为，零回归）。三者字符集互斥，
      无优先级歧义。
    - ``absent`` 存在且非 false → ``[AbsenceJudger()]``，与其他 kwarg 互斥
      （同时声明视为配置错误，因为语义矛盾：不存在的元素无法再判定其他属性）。
    - ``absent=false`` 显式声明 → 等价于默认 ``[ExistenceJudger()]``。
    - 其余 kwarg → 按声明顺序映射到判定器实例（同一判定器多个 kwarg 只生成一个实例，去重），
      未知 kwarg 直接报错（快速失败，避免用例拼写错误被静默忽略）。
    """
    keys = [k for k in spec.kwargs if k not in _RESERVED_KEYS and k not in _MODIFIER_KEYS]
    _validate_numeric_keys(keys)
    if not keys:
        if is_path_like(spec.description):
            return [PathExistsJudger()]
        if is_var_expr(spec.description):
            return [VarExprJudger()]
        return [ExistenceJudger()]

    if "absent" in keys:
        if len(keys) > 1:
            raise JudgerDispatchError(
                "absent= 与其他判定 kwarg 互斥（不存在的元素无法再判定其他属性）"
            )
        if _is_falsy(spec.kwargs["absent"]):
            return [ExistenceJudger()]
        return [AbsenceJudger()]

    judgers: list[JudgerBase] = []
    seen: set[type] = set()
    unknown: list[str] = []
    for key in keys:
        cls = _match_judger_cls(key)
        if cls is None:
            unknown.append(key)
            continue
        if cls not in seen:
            seen.add(cls)
            judgers.append(cls())

    if unknown:
        raise JudgerDispatchError(f"未知的断言 kwarg: {unknown}")

    return judgers


def needs_element(kwargs: dict[str, str], description: str = "") -> bool:
    """判断该 kwargs 组合是否需要先 resolve 元素。

    url/title/js 等"无元素类"判定可省略 description，不需要 resolve；
    其余（包括默认 ExistenceJudger、absent、contains/count/attribute/class）均需要。

    ``description`` 为可选参数（默认空串，向后兼容既有单参调用点）：裸写法
    （无 kwarg）命中路径特征时不需要 resolve 元素（不应该拿路径字符串去找 UI
    元素），见 docs/plans/archive/02-dsl-actions-实施纪要.md；命中变量比较/
    判空函数调用表达式时同理不需要 resolve（不应该拿 "equal(a, b)" 这类表达式
    去找 UI 元素），见 docs/plans/archive/02-dsl-actions-实施纪要.md。
    """
    keys = [k for k in kwargs if k not in _RESERVED_KEYS and k not in _MODIFIER_KEYS]
    _validate_numeric_keys(keys)
    if not keys:
        if description and (is_path_like(description) or is_var_expr(description)):
            return False
        return True
    return not all(k in _NO_ELEMENT_KEYS for k in keys)


__all__ = [
    "AbsenceJudger", "AttributeJudger", "ClassJudger", "ContentJudger",
    "CountJudger", "ExistenceJudger", "JsJudger", "JudgeResult", "JudgerBase",
    "JudgerDispatchError", "JudgerSpec", "NumericJudger", "PathExistsJudger", "PromptJudger",
    "SelectorJudger", "TitleJudger", "UrlJudger", "VarExprJudger",
    "is_path_like", "is_var_expr", "needs_element", "select_judgers",
]
