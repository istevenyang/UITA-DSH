"""
core/runner/condition_eval.py — if/elif/else 条件求值 + IfNode 展开（2026-07-25
新增，见 docs/plans/archive/02-dsl-actions-实施纪要.md）。

被 core/interpreter.py 与 core/runner/streaming_runner.py 共同复用（§3.7 风险 2：
避免两个 Runner 各自实现一套条件求值/分支选择逻辑产生分叉）。两个 Runner 只需：
  1. 定义一个绑定了自身 engine/resolver/case_ctx 的 `evaluate(cond) -> bool` 闭包；
  2. 把既有的 `for node in step.body:` 循环头替换为
     `async for node in iter_effective_body(step.body, evaluate):`
即可获得 IfNode 支持，循环体内部对 ActionNode/WaitNode/AssertNode/SetNode 的
既有分发逻辑（截图、page-hash 校验、switch 同步等）完全不需要改动——
`iter_effective_body()` 在迭代到 IfNode 时才求值其条件（惰性展开，保证按
源码书写顺序、前序节点先执行完才求值后续 if 条件的语义），命中分支的子
body 递归展开后原样并入迭代序列，调用方感知不到 IfNode 的存在。
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from uita.core.actions.base import ActionContext
from uita.core.actions.common.action_assert import split_or_targets
from uita.core.actions.judgers import JudgerDispatchError, needs_element, select_judgers
from uita.core.actions.judgers.base import JudgerSpec
from uita.core.dsl.nodes import ConditionNode, IfNode, StepBodyNode
from uita.core.dsl.parser import parse_condition_expr

if TYPE_CHECKING:
    from uita.core.engine.base import EngineBase
    from uita.core.resolver.resolver import Resolver
    from uita.core.runner.case_context import CaseContext

logger = logging.getLogger(__name__)

EvaluateFn = Callable[[ConditionNode], Awaitable[bool]]

_RE_VAR_REF = re.compile(r"^\$\{([^}]+)\}$")


async def evaluate_condition(
    cond: ConditionNode,
    engine: "EngineBase",
    resolver: "Resolver",
    case_ctx: Optional["CaseContext"] = None,
) -> bool:
    """求值单个 if/elif 条件（瞬时快照判断，不等待/不重试），返回已应用
    `negate` 修饰后的最终布尔结果。

    条件求值失败（元素不存在、kwarg 配置错误、判定异常等）统一视为内部
    结果 False（再叠加 negate），不向上抛异常、不算用例失败——由调用方
    继续走下一个 elif/else 分支或跳过整个 if 块（见计划 §3.5）。
    """
    try:
        if cond.is_empty_check:
            raw_result = _evaluate_empty(cond, case_ctx)
        else:
            raw_result = await _evaluate_judger(cond, engine, resolver)
    except Exception as exc:  # noqa: BLE001 — 条件求值异常按 False 处理，不向上抛出
        logger.debug("[if/elif] 条件求值异常（视为 False）: %r — %s", cond.raw_expr, exc)
        raw_result = False

    return (not raw_result) if cond.negate else raw_result


def _evaluate_empty(cond: ConditionNode, case_ctx: Optional["CaseContext"]) -> bool:
    """empty[${var}] 判空：变量未定义（None）或空字符串 → True。

    直接调用 case_ctx.get(var_name)，而非 render() 字符串替换再对比原文
    （更精确，见计划 §3.4 表格结论）。parser 阶段已校验 raw_expr 必为
    ``${var}`` 形式，此处按该约定解析变量名。
    """
    m = _RE_VAR_REF.match(cond.raw_expr.strip())
    var_name = m.group(1).strip() if m else cond.raw_expr.strip()
    value = case_ctx.get(var_name) if case_ctx is not None else None
    return value is None or value == ""


async def _evaluate_judger(cond: ConditionNode, engine: "EngineBase", resolver: "Resolver") -> bool:
    """非 empty 条件求值：复用 core/actions/judgers 体系（同 assert[] kwarg 语法）。

    rosetta_map js 表达式元素早查表短路（2026-07-28 新增，见宪法 R22b，与
    `core/actions/common/action_assert.py` 对称）：`if[<element_name>]`/
    `elif[<element_name>]` 裸括号写法（kwargs 为空）若该元素声明了 js 表达式
    字段，直接注入 kwargs={"js": ...}，复用 JsJudger；未命中则维持原逻辑。

    OR 逻辑（2026-08-06 新增，与 `action_assert.py` 对称，见
    docs/plans/archive/02-dsl-actions-实施纪要.md）：`if[A|B]`/`elif[A|B]`
    用 `split_or_targets()` 拆成候选，逐候选走单趟（非 poll）判定，任一候选
    全部 judger 通过即整体 True；某候选 kwarg 派发失败按现状视为 False 并
    跳过该候选，继续尝试其余候选；全部候选失败才返回 False。无 `|` 时候选
    只有一个，逐候选逻辑退化为原有单候选路径（零回归）。
    """
    base_description, base_kwargs = parse_condition_expr(cond.raw_expr)

    for description in split_or_targets(base_description):
        kwargs = dict(base_kwargs)

        if not kwargs and description:
            try:
                js_expr = resolver.get_js_expr(description)
            except Exception:  # noqa: BLE001 — 查表异常视为未命中，维持原逻辑
                js_expr = None
            if js_expr is not None:
                kwargs = {"js": js_expr}

        try:
            spec = JudgerSpec(description=description, kwargs=kwargs)
            judgers = select_judgers(spec)
        except JudgerDispatchError as exc:
            logger.debug("[if/elif] 条件 kwarg 派发失败（视为 False）: %s", exc)
            continue

        resolve_result = None
        if needs_element(kwargs, description):
            try:
                resolve_result = await resolver.resolve(description)
            except Exception as exc:  # noqa: BLE001 — resolve 异常视为未命中
                logger.debug("[if/elif] 条件 resolve 异常（视为未命中）: %s", exc)
                resolve_result = None

        ctx = ActionContext(engine=engine, resolver=resolver, args=[description], kwargs=kwargs)
        all_ok = True
        for judger in judgers:
            r = await judger.check(ctx, resolve_result, spec)
            if not r.ok:
                all_ok = False
                break
        if all_ok:
            return True

    return False


async def select_if_branch(node: IfNode, evaluate: EvaluateFn) -> list[StepBodyNode]:
    """按 if → elif* 顺序求值，返回首个命中分支的 body；全部未命中则返回
    ``else_body``（无 else 时为空列表，等价于该 IfNode 整体无操作）。
    """
    for cond, body in node.branches:
        if await evaluate(cond):
            return body
    return node.else_body


async def iter_effective_body(
    body: list[StepBodyNode], evaluate: EvaluateFn,
):
    """惰性展开 step.body 中的 IfNode，按源码顺序产出真正需要执行的叶子节点
    （ActionNode/WaitNode/AssertNode/SetNode），供两个 Runner 的既有
    ``for node in step.body:`` 循环直接消费（改成 ``async for``）。

    条件求值发生在"迭代到该 IfNode 时"，而非提前一次性展开整个列表——保证
    "前面的 action 先执行完，if 条件才求值"的顺序语义（快照判断基于当时的
    最新页面/变量状态，不是基于 step 开始时的状态）。
    """
    for node in body:
        if isinstance(node, IfNode):
            branch_body = await select_if_branch(node, evaluate)
            async for sub in iter_effective_body(branch_body, evaluate):
                yield sub
        else:
            yield node
