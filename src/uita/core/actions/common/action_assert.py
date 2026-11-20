"""
assert: [element_name]                                — 存在 + 可见（默认，零回归）
assert: [element_name, visible=false]                  — 仅断言存在（跳过可见性校验）
assert: [element_name, contains="xxx", count_min=1]    — judgers 体系（P2 新增）
assert: [absent=true]                                  — 断言元素不存在
assert: [A|B]                — OR 逻辑：A/B 任一候选判定
                                                          通过即整体通过（2026-08-06
                                                          新增，见宪法R22c同款 `|`
                                                          多候选语义；kwargs 对每个
                                                          候选独立生效）
verify: [element_name]                                 — assert 别名

visible=false 用途：disabled/未启用态控件（如未选文件时的"立即清理按钮"、
disabled 下拉框等）在 AX/UIA 树中确实存在，但引擎 is_visible 因 enabled=False
返回 False，默认 assert 会误判"存在但不可见"而失败；此时用 visible=false 只校验
元素已注册/渲染，不校验可交互性。见 core/actions/judgers/existence.py。

无 kwarg 时行为与改造前完全一致：resolve 一次，未找到直接 fail；找到后
ExistenceJudger 检查可见性，poll 直到超时（默认预算约等价于改造前的 3 次
150ms 重试）。有 kwarg 时按 core/actions/judgers 体系派发（AND 组合）。

**rosetta_map js 表达式元素早查表短路**（2026-07-28 新增，见宪法 R22b）：
`assert[<element_name>]`/`verify[<element_name>]` 裸括号写法（kwargs 为空）
若该元素在 rosetta_map 中声明了 `js` 表达式字段（`Resolver.get_js_expr()`
命中），直接注入 `kwargs={"js": ...}`，等价于 `assert[js=<js表达式>]`，复用
现有 `JsJudger`（含变量引用能力），不需要先 resolve 元素。未命中
（`get_js_expr()` 返回 `None`）则维持原有 `ExistenceJudger`/`PathExistsJudger`/
`VarExprJudger` 分发逻辑不变（零回归）。`if[<element_name>]`/`elif[]` 对称
支持见 `core/runner/condition_eval.py::_evaluate_judger()`。

见 docs/plans/archive/02-dsl-actions-实施纪要.md、
docs/plans/archive/03-resolver-rosetta-实施纪要.md、
docs/plans/archive/02-dsl-actions-实施纪要.md。
"""
from __future__ import annotations

import asyncio
import time

from uita.core.actions.base import ActionBase, ActionContext, ActionResult
from uita.core.actions.judgers import JudgerDispatchError, needs_element, select_judgers
from uita.core.actions.judgers.base import JudgerSpec
from uita.core.actions.registry import register_action
from uita.core.dsl.nodes import AssertNode
from uita.core.logging_manager import log_action_execution

# 默认 poll 预算：约等价于改造前"3 次重试 * 150ms 间隔"的总耗时（零回归）。
DEFAULT_TIMEOUT_S = 0.45
POLL_INTERVAL_S = 0.15

# prompt= 视觉断言默认预算：AI 视觉判定耗时远超普通结构化判定（通常数秒~
# 数十秒），未显式声明 timeout= 时使用该默认值，仅对含 prompt 键的场景生效，
# 不影响其余判定器既有默认值。见 docs/plans/archive/02-dsl-actions-实施纪要.md。
VISUAL_DEFAULT_TIMEOUT_S = 60.0


def split_or_targets(element_name: str) -> list[str]:
    """将 `assert[A|B]` 的 target 拆成候选列表，供 OR 逻辑消费（2026-08-06 新增，
    见 docs/plans/archive/02-dsl-actions-实施纪要.md）。

    - 无 `|` → 原样返回 `[element_name]`（零回归保证，包括空字符串场景）。
    - 有 `|` → 按 `|` 切分并 strip 各候选、过滤空候选；全部为空则退化回
      `[element_name]`（防御性兜底，避免空列表）。
    """
    if not element_name or "|" not in element_name:
        return [element_name]
    parts = [p.strip() for p in element_name.split("|")]
    parts = [p for p in parts if p]
    return parts or [element_name]


def _parse_timeout(kwargs: dict[str, str]) -> float:
    raw = kwargs.get("timeout", "")
    if not raw:
        return VISUAL_DEFAULT_TIMEOUT_S if "prompt" in kwargs else DEFAULT_TIMEOUT_S
    v = raw.strip().lower()
    try:
        if v.endswith("ms"):
            return float(v[:-2]) / 1000.0
        if v.endswith("s"):
            return float(v[:-1])
        return float(v)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@register_action("assert", "verify")
class AssertAction(ActionBase):
    @property
    def name(self) -> str:
        return "assert"

    async def execute(self, ctx: ActionContext) -> ActionResult:
        # ctx.extra 可能携带预解析的 AssertNode（Interpreter/StreamingRunner 均如此传入）
        assert_node: AssertNode | None = ctx.extra.get("assert_node")
        t0 = time.monotonic()

        # 标记本次判定来自 assert[]/verify[]（区别于 if[]/elif[] 条件求值，见
        # core/runner/condition_eval.py::_evaluate_judger() 不设置该标记）。
        # 供 L3.8b assert_fingerprint 等"仅 assert 触发"的 fallback_chain 层读取，
        # 见 docs/plans/archive/03-resolver-rosetta-实施纪要.md。
        ctx.extra["assert_call"] = True

        if assert_node is not None:
            element_name = assert_node.target
            kwargs = dict(assert_node.kwargs)
        elif ctx.args:
            element_name = ctx.args[0].strip()
            kwargs = dict(ctx.kwargs) if ctx.kwargs else {}
        else:
            return ActionResult.fail("assert: no target specified")

        # ctx.kwargs 优先级低于 assert_node.kwargs（后者是 DSL 显式解析结果），
        # 但仍合并进来以兼容直接构造 ActionContext(kwargs=...) 的调用方式（如测试）。
        if ctx.kwargs:
            kwargs = {**ctx.kwargs, **kwargs}

        # OR逻辑（2026-08-06 新增，见模块 docstring）：element_name 含 `|` 时拆
        # 成多候选，每个候选独立走 js 早查表短路 + judger 派发；无 `|` 时
        # targets 恰好是 [element_name]，下方逐候选逻辑退化为单候选路径，与
        # 改造前完全一致（零回归）。
        targets = split_or_targets(element_name)
        has_pipe = len(targets) > 1

        candidates: list[dict] = []
        dispatch_errors: list[str] = []
        for name in targets:
            # rosetta_map js 表达式早查表短路（见模块 docstring）：仅裸括号写法
            # （kwargs 为空）才短路，已显式声明其他 kwarg 的写法不受影响。
            cand_kwargs = dict(kwargs)
            if not cand_kwargs and name:
                try:
                    js_expr = ctx.resolver.get_js_expr(name)
                except Exception:  # noqa: BLE001 — 查表异常视为未命中，维持原逻辑
                    js_expr = None
                if js_expr is not None:
                    cand_kwargs = {"js": js_expr}

            try:
                spec = JudgerSpec(description=name, kwargs=cand_kwargs)
                judgers = select_judgers(spec)
            except JudgerDispatchError as exc:
                if not has_pipe:
                    # 无 `|` 时行为与改造前完全一致：派发失败直接 fail。
                    return ActionResult.fail(
                        f"assert: {exc}", duration_ms=(time.monotonic() - t0) * 1000,
                    )
                dispatch_errors.append(f"{name}: {exc}")
                continue

            candidates.append({
                "name": name,
                "kwargs": cand_kwargs,
                "spec": spec,
                "judgers": judgers,
                "require_element": needs_element(cand_kwargs, name),
                "resolve_result": None,
            })

        if not candidates:
            # OR 全部候选派发失败（仅当 has_pipe 时才可能走到这里）。
            reason = "; ".join(dispatch_errors)
            return ActionResult.fail(
                f"assert: {reason}", duration_ms=(time.monotonic() - t0) * 1000,
            )

        # 消息格式是否标注候选名：候选数=1（无 `|`，或 OR 全部预分发失败只剩 1
        # 个）时与改造前字符不差；候选数>1 时才追加候选标注。
        multi = len(candidates) > 1

        # 注意：resolve 延迟到 while 循环内按候选顺序懒执行（而非此处提前
        # 逐个 resolve 全部候选），保证"候选 1 通过 → 立即 ok"时不会多余
        # resolve 候选 2（短路，见计划文档 §测试策略 2）；单候选（无 `|`）时
        # 与改造前 resolve 调用次数完全一致（零回归）。

        # timeout 基于原始合并 kwargs 解析（OR 各候选共用同一截止时间，见计划文档）。
        timeout_s = 0.0 if ctx.extra.get("repair_mode") else _parse_timeout(kwargs)
        deadline = t0 + timeout_s

        # 外层 dispatch（Interpreter/StreamingRunner，为截图标注取坐标）可能已
        # 对部分候选执行过一次 resolve，通过 ctx.extra["prewarmed_resolve"]
        # （dict[name, ResolveResult]）传入，首轮复用可省去重复的全链路
        # resolve（见 docs/plans/20260812-assert-dedup-resolve.md）。用 pop()
        # 一次性消费：仅首轮生效，poll 超时重试的后续轮次仍强制真实 resolve，
        # 避免用陈旧结果误判元素状态变化（零回归超时/重试语义）。
        prewarmed_resolve: dict = ctx.extra.get("prewarmed_resolve") or {}

        while True:
            fail_reasons: list[tuple[str, str]] = []
            for cand in candidates:
                # 若需要元素但尚未 resolve 到，先尝试一轮 resolve
                if cand["require_element"] and (
                    cand["resolve_result"] is None or not cand["resolve_result"].found
                ):
                    _pre = prewarmed_resolve.pop(cand["name"], None)
                    cand["resolve_result"] = (
                        _pre if _pre is not None else await ctx.resolver.resolve(cand["name"])
                    )

                all_ok = True
                reason = ""
                for judger in cand["judgers"]:
                    r = await judger.check(ctx, cand["resolve_result"], cand["spec"])
                    if not r.ok:
                        all_ok, reason = False, r.reason
                        break

                if all_ok:
                    name = cand["name"]
                    if multi:
                        msg = f"assert passed: '{name}'（命中: {name}）" if name else "assert passed"
                    else:
                        msg = f"assert passed: '{name}'" if name else "assert passed"
                    dur = (time.monotonic() - t0) * 1000
                    res_obj = cand.get("resolve_result")
                    lvl = getattr(getattr(res_obj, "level", None), "name", "") if res_obj else ""
                    log_action_execution("assert", name or element_name, passed=True, duration_ms=dur, level=lvl, message=msg)
                    result = ActionResult.ok(msg, duration_ms=dur)
                    result.extra["existence_strategy"] = ctx.extra.get("existence_strategy", "structured")
                    return result

                fail_reasons.append((cand["name"], reason))

            if time.monotonic() >= deadline:
                if multi:
                    combined = "; ".join(f"{n}: {r}" for n, r in fail_reasons)
                else:
                    combined = fail_reasons[0][1] if fail_reasons else ""
                dur = (time.monotonic() - t0) * 1000
                msg = f"assert 失败: {combined}"
                log_action_execution("assert", element_name, passed=False, duration_ms=dur, message=msg)
                result = ActionResult.fail(msg, duration_ms=dur)
                result.extra["existence_strategy"] = ctx.extra.get("existence_strategy", "structured")
                return result

            await asyncio.sleep(POLL_INTERVAL_S)
