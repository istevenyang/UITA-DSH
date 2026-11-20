"""
Resolver — L1~L9 统一整数层级元素解析与自愈降级链。

统一降级层级（L1 ~ L9）：
  L1 MAP_EXACT    : rosetta_map 精确 key → engine.find_by_id(selector)
  L2 SEMANTIC     : 语义近似匹配（别名/包含/bigram）+ 界面核对（count==1）
  L3 TEXT_MATCH   : engine.find_by_text(element_name)
  L4 LABEL_MATCH  : 关联 <label> 文本匹配
  L5 ROLE_MATCH   : engine.find_by_role / find_all_by_role
  L6 LLM_SELECTOR : 基于可访问性摘要的 LLM 选择器推断
  L7 VISUAL_FP    : OpenCV 视觉指纹匹配（core/resolver/l7_visual_fingerprint.py）
  L8 LLM_VISUAL   : 视觉大模型坐标定位与判定
  L9 QT_PROBE     : Qt 进程内对象树探针通道（按需由自定义降级链开启）
"""
from __future__ import annotations

import logging
import re
import time

import uita.config as settings
from uita.core.logging_manager import log_locate_attempt, log_route_decision
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from uita.core.engine.base import ElementRef, EngineBase
from uita.core.platform.element_map_loader import ElementMapLoader
from uita.core.platform.router_proxy_loader import RouterProxyLoader
from uita.core.platform.window_hint import WindowHint
from uita.core.resolver.router import ActionRouter, ResolveStrategy, build_effective_chain, parse_proxy_tokens

logger = logging.getLogger(__name__)


class _NullAsyncScope:
    """空异步上下文：无 window_hint / 引擎无 window_scope 能力时的占位。"""

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False

# 用于从元素名推断"当前页面"的语义关键词
_PAGE_HINT_KEYWORDS = ("标题", "页面", "主界面", "主页", "首页")


class ResolveLevel(Enum):
    L1_MAP_EXACT    = "L1_MAP_EXACT"
    L2_SEMANTIC     = "L2_SEMANTIC"
    L3_TEXT_MATCH   = "L3_TEXT_MATCH"
    L4_LABEL_MATCH  = "L4_LABEL_MATCH"
    L5_ROLE_MATCH   = "L5_ROLE_MATCH"
    L6_LLM_SELECTOR = "L6_LLM_SELECTOR"
    L7_VISUAL_FP    = "L7_VISUAL_FP"
    L8_LLM_VISUAL   = "L8_LLM_VISUAL"
    L9_QT_PROBE     = "L9_QT_PROBE"
    NOT_FOUND       = "NOT_FOUND"


@dataclass
class ResolveResult:
    element: ElementRef | None
    level: ResolveLevel
    element_name: str = ""
    # 自愈回写元数据（L3/L4/L5/L6 成功时填充，供 _self_heal_writeback 使用）
    heal_text: str = ""          # L3: 用于匹配的文本
    heal_role: str = ""           # L5: 用于匹配的 role
    heal_role_name: str = ""      # L5: 用于匹配的 role_name
    heal_label: str = ""          # L4: 用于匹配的 label 文本
    fallback_strategy: str = ""   # LLM 成功后可复用的稳定策略（TEXT_MATCH/ROLE_MATCH等）
    heal_llm_prompt: str = ""     # 受限长度LLM请求提示词；不保存响应、截图或密钥

    @property
    def found(self) -> bool:
        return self.element is not None


NOT_FOUND = ResolveResult(element=None, level=ResolveLevel.NOT_FOUND)


def _build_l4_locate_prompt(element_name: str, usage: str, step_context: str) -> str:
    """L4 LLM 视觉定位的 prompt 内容构建：元素描述 + usage 说明 + 当前 step。

    usage/step_context 为空时自然跳过对应行，退化为仅含 element_name 的
    原始内容（与改造前 locate(element_name, screenshot) 等价）。

    计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md
    """
    lines = [f"目标元素：{element_name}"]
    if usage:
        lines.append(f"元素用途说明：{usage}")
    if step_context:
        lines.append(f"当前测试步骤：{step_context}")
    lines.append(
        "重要规则：必须精确匹配上述目标元素；找不到精确对应的元素时返回"
        "“不存在该描述位置”；禁止退而求其次挑语义相近的其他元素。"
    )
    return "\n".join(lines)


def _get_semantic_threshold() -> float:
    try:
        from uita.config import SEMANTIC_MATCH_THRESHOLD
        return SEMANTIC_MATCH_THRESHOLD
    except ImportError:
        return 0.5


class Resolver:
    def __init__(
        self,
        engine: EngineBase,
        element_map: ElementMapLoader,
        overlay_candidates: Optional[list[str]] = None,
        overlay_ignore: Optional[set[str]] = None,
        router: Optional[ActionRouter] = None,
        fingerprint_resolver: Optional[object] = None,
        vision_client: Optional[object] = None,
        router_proxy_loader: Optional[RouterProxyLoader] = None,
        router_proxy_candidate_store: Optional[object] = None,
        rosetta_map_view: Optional[object] = None,
        fallback_view: Optional[object] = None,
        router_proxy_view: Optional[object] = None,
        default_strategy_chain: Optional[list[str] | str] = None,
    ):
        self._engine = engine
        self._element_map = element_map
        self._page_context: str = ""   # 当前页面上下文，供 L2 缩窄使用
        # L8 LLM prompt 富化：当前 step 名称（与 page_context 同构但独立）
        self._step_context: str = ""
        self._router = router or ActionRouter()
        self._router_proxy_loader = router_proxy_loader
        self._router_proxy_candidate_store = router_proxy_candidate_store
        if isinstance(default_strategy_chain, str):
            self._default_strategy_chain = [
                tok.strip().upper() for tok in default_strategy_chain.split(",") if tok.strip()
            ]
        elif isinstance(default_strategy_chain, (list, tuple)):
            self._default_strategy_chain = [
                str(tok).strip().upper() for tok in default_strategy_chain if str(tok).strip()
            ]
        elif hasattr(element_map, "default_strategy_chain"):
            self._default_strategy_chain = list(element_map.default_strategy_chain() or [])
        else:
            self._default_strategy_chain = []

        # ── DOM 覆盖层感知（active_container）──────────────────────
        self._active_container: Optional[str] = None
        self._overlay_candidates: list[str] = overlay_candidates or []
        self._overlay_ignore: set[str] = overlay_ignore or set()

        # ── L7 视觉指纹 + L8 LLM 视觉（均默认 None，零回归）─────────
        # fingerprint_resolver: core.resolver.l7_visual_fingerprint.VisualFingerprintResolver
        # vision_client: core.drivers.llms.base.VisionLLMClient
        self._fingerprint_resolver = fingerprint_resolver
        self._vision_client = vision_client

        # ── DB 三表 ORM View（自愈闭环写入，默认 None，DB 不可用时静默跳过）───
        self._rosetta_map_view = rosetta_map_view   # Optional[RosettaMapView]
        self._fallback_view = fallback_view         # Optional[FallbackView]
        self._router_proxy_view = router_proxy_view # Optional[RouterProxyView]

        # ── 延迟提交缓冲（用例级，默认 None → 立即写 DB，零回归）────────
        # 由 streaming_runner 在用例开始时注入，用例成功后 commit()，失败后 discard()。
        # heal_buffer is None 时，_maybe_record_candidate() 立即写 DB（原有行为）。
        self._heal_buffer: Optional[object] = None  # Optional[HealBuffer]
        self._repair_mode = False

    # ── page_context 管理 ─────────────────────────────────────────────

    @property
    def vision_client(self) -> Optional[object]:
        """视觉客户端（DshVisionClient / CustomVisionClient）。

        供 Judger 层（ContentJudger 等）在元素定位失败后做视觉兜底判定。
        未注入时返回 None。
        """
        return self._vision_client

    @property
    def has_vision_client(self) -> bool:
        return self._vision_client is not None

    def set_heal_buffer(self, buf: Optional[object]) -> None:
        """注入用例级自愈缓冲（由 streaming_runner 在用例开始时调用）。

        buf=None → 清空缓冲，恢复立即写 DB 模式（用例结束后调用）。
        """
        self._heal_buffer = buf

    def set_repair_mode(self, enabled: bool) -> None:
        """Use only TCS-declared or original Rosetta selectors; never heal/fallback."""
        self._repair_mode = bool(enabled)

    def set_page_context(self, page: str) -> None:
        """外部显式设置当前页面上下文（推荐：Interpreter 在 wait 步骤成功后调用）。"""
        if page and page != self._page_context:
            logger.debug("[Resolver] page_context 更新: %r → %r", self._page_context, page)
            self._page_context = page

    def set_step_context(self, step_name: str) -> None:
        """外部显式设置当前 step 名称，供 L8 构建 LLM prompt 使用。

        由 Interpreter._run_step()/streaming_runner._run_step_body() 在每个
        step 开始执行前调用。与 page_context 语义不同（step 是"当前动作"，
        page 是"当前页面"），不复用同一字段，避免隐式耦合 L2 缩窄逻辑。
        """
        self._step_context = step_name or ""

    @property
    def step_context(self) -> str:
        return self._step_context

    def get_element_usage(self, element_name: str) -> str:
        """从 rosetta_map 查找元素 usage 说明，供 L8 构建 LLM prompt 使用。

        未命中 rosetta_map 或该元素无 usage 字段时返回空字符串（调用方按空
        字符串处理，prompt 退化为不含 usage 说明的原始内容）。

        计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md
        """
        entry = self._element_map.find(element_name)
        return (entry.usage if entry else "") or ""

    def get_js_expr(self, element_name: str) -> Optional[str]:
        """rosetta_map 中该元素若声明了 js 表达式字段，返回其内容，否则 None
        （元素未注册 / 已注册但只有 selector/mac/win 定位字段，两种情况均返回
        None，调用方需要区分时配合 has_locator_entry() 判断）。

        纯字典查找，零 IO。供 assert[<element_name>]/if[<element_name>]/
        eval_js[<element_name>] 元素名 JS 表达式查找复用（见宪法 R22b）。
        """
        entry = self._element_map.find(element_name)
        return entry.js_expr if entry and entry.js_expr else None

    def has_locator_entry(self, element_name: str) -> bool:
        """rosetta_map 中该元素是否已注册为"可定位元素"（entry.selector 非空）。

        供 eval_js[] 区分"未注册"（按字面量 JS 执行）与"已注册为定位元素但无
        js 表达式"（报错）两种不同语义。
        """
        entry = self._element_map.find(element_name)
        return bool(entry and entry.selector)

    def update_page_context_from_name(self, element_name: str) -> None:
        """根据元素名自动推断页面上下文：含页面语义关键词时更新 _page_context。

        例："开机启动项页面标题" → _page_context = "开机启动项页面标题"
        Interpreter 在每次成功的 wait/assert 后调用，无需手动追踪页面跳转。
        """
        if any(kw in element_name for kw in _PAGE_HINT_KEYWORDS):
            self.set_page_context(element_name)

    # ── DOM 覆盖层感知（active_container）──────────────────────────────

    async def on_click_start(self) -> set[str]:
        """click 前调用：快照当前可见覆盖层集合。

        仅 PlaywrightEngine（有 snapshot_overlay_selectors 方法）生效，其余
        引擎（Mac/Win 原生）直接返回空集合，零副作用。
        """
        if hasattr(self._engine, "snapshot_overlay_selectors"):
            try:
                return await self._engine.snapshot_overlay_selectors(
                    self._overlay_candidates, self._overlay_ignore
                )
            except Exception as exc:
                logger.debug("[overlay] snapshot_overlay_selectors 异常（忽略）: %s", exc)
        return set()

    async def on_click_end(self, before_snapshot: set[str]) -> None:
        """click 后调用：检测新出现的覆盖层并更新 _active_container。"""
        if not hasattr(self._engine, "detect_new_overlay"):
            return
        try:
            new_overlay = await self._engine.detect_new_overlay(
                self._overlay_candidates, before_snapshot, self._overlay_ignore
            )
        except Exception as exc:
            logger.debug("[overlay] detect_new_overlay 异常（忽略）: %s", exc)
            return
        if new_overlay:
            logger.debug("[overlay] 检测到新覆盖层: %s", new_overlay)
            self._active_container = new_overlay

    async def _try_scoped(self, element_name: str, container: str) -> ResolveResult:
        """在 active_container scope 内尝试用 L1 selector 查找（仅 Web 引擎）。"""
        entry = self._element_map.find(element_name)
        if entry is None or not entry.selector:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        try:
            element = await self._engine.find_by_id_scoped(entry.selector, container)
        except Exception as exc:
            logger.debug("[overlay] find_by_id_scoped 异常（忽略）: %s", exc)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        if element is None:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        logger.debug("[overlay] resolved '%s' scoped in '%s'", element_name, container)
        return ResolveResult(element=element, level=ResolveLevel.L1_MAP_EXACT, element_name=element_name)

    # ── 解析主入口 ────────────────────────────────────────────────────

    async def resolve(self, element_name: str, *, skip_l4: bool = False, local_only: bool = False) -> ResolveResult:
        """两级降级入口：active_container scope 优先 → 全局 L1→L2→L3→L4→L5→L6→L7→L8。

        _active_container 默认为 None，未触发过 click 覆盖层的用例（或非 Web
        引擎）该分支恒不触发，行为与改造前完全一致。

        Args:
            element_name: 目标元素名称
            skip_l4:      为 True 时跳过 L4 LLM_VISUAL（截图标注专用，避免触发 VL 调用）
            local_only:   为 True 时跳过全部需 HTTP/远程调用的策略（LLM_SELECTOR +
                          LLM_VISUAL），仅走本地策略（MAP_EXACT/SEMANTIC/ROLE_MATCH/
                          VISUAL_FP）。用于"探测元素是否存在"的高频轻量场景
                          （如 open[] 复位探测），避免每次探测都付 LLM HTTP 耗时
                          （真机实测 ~3s/次，4 个探测元素 = ~15-20s 纯浪费，
                          见 docs/plans/20260722153000-open-reset-local-only-probe.md）。
        """
        if self._repair_mode:
            return await self._l1_map_exact(element_name)

        # 覆盖层存活性校验：已消失则清空，回退全局
        if self._active_container and hasattr(self._engine, "is_overlay_gone"):
            try:
                gone = await self._engine.is_overlay_gone(self._active_container)
            except Exception as exc:
                logger.debug("[overlay] is_overlay_gone 异常（忽略，视为已消失）: %s", exc)
                gone = True
            if gone:
                logger.debug("[overlay] 覆盖层已消失，清空 active_container: %s", self._active_container)
                self._active_container = None

        if self._active_container and hasattr(self._engine, "find_by_id_scoped"):
            scoped = await self._try_scoped(element_name, self._active_container)
            if scoped.found:
                return scoped
            # scope 内未命中 → 回退全局（不清空 active_container，允许后续再次尝试）

        return await self._resolve_global(element_name, skip_l4=skip_l4, local_only=local_only)

    async def _resolve_global(self, element_name: str, *, skip_l4: bool = False, local_only: bool = False) -> ResolveResult:
        """按人工链、DB default、DB fallback、标准全链依次定位。

        每条链都是一次完整的去重降级链：DB 路由的有效首策略放在 L1 后，失败后
        继续该链的剩余标准步骤，不重复启动第二条相同标准链。所有来源的策略均先
        按当前元素的平台指纹和已装配能力裁剪。
        """
        entry = self._entry_for_resolution(element_name)
        is_native = not hasattr(self._engine, "find_by_id_scoped")

        proxy_entry = None
        default_proxy_entry = None
        if self._router_proxy_loader is not None:
            if entry is not None and entry.id:
                proxy_entry = self._router_proxy_loader.find(entry.id)
            default_proxy_entry = self._router_proxy_loader.default_route()

        # ── 优先级 1：人工元素专属链（router_proxy.json 元素级或 rosetta_map strategy_chain）──
        has_elem_custom = bool(
            (proxy_entry and proxy_entry.effective)
            or (entry is not None and getattr(entry, "strategy_chain", None))
        )
        if has_elem_custom:
            decision = self._router.route_v2(
                element_name, entry,
                router_proxy_entry=proxy_entry,
                default_proxy_entry=None,
                is_native=is_native,
            )
            decision.strategies, skip_infos = self._build_capability_filtered_chain(
                decision.strategies, entry, is_native,
            )
            suffix = f"; {'; '.join(skip_infos)}" if skip_infos else ""
            logger.debug(
                "[Router] '%s' → %s (定制全链%s)",
                element_name, [s.value for s in decision.strategies], suffix,
            )
            log_route_decision(
                element_name, decision.strategies, "元素自定义级(定制全链)", decision.reason, skip_infos,
            )
            return await self._try_chain(
                decision, entry, element_name, skip_l4=skip_l4, local_only=local_only, idx_offset=0,
            )

        # ── 优先级 1.5：DB promote/default 路径 ───────────────────────
        default_route = self._get_db_route(entry, source="default", is_native=is_native)
        if default_route:
            chain, _ = self._build_capability_filtered_chain(
                self._db_route_configured(default_route), entry, is_native,
            )
            logger.debug(
                "[Router] '%s' → DB default: %s",
                element_name, [s.value for s in chain],
            )
            result = await self._try_chain_strategies(
                chain, entry, element_name,
                skip_l4=skip_l4, local_only=local_only, idx_offset=0,
            )
            if not result.found:
                logger.warning("[NOT_FOUND] could not resolve element: '%s'", element_name)
            return result

        # ── 优先级 2：DB fallback 路径 ─────────────────────────────────
        fallback_route = self._get_db_route(entry, source="fallback", is_native=is_native)
        if fallback_route:
            chain, _ = self._build_capability_filtered_chain(
                self._db_route_configured(fallback_route), entry, is_native,
            )
            logger.debug(
                "[Router] '%s' → DB fallback: %s",
                element_name, [s.value for s in chain],
            )
            result = await self._try_chain_strategies(
                chain, entry, element_name,
                skip_l4=skip_l4, local_only=local_only, idx_offset=0,
            )
            if not result.found:
                logger.warning("[NOT_FOUND] could not resolve element: '%s'", element_name)
            return result

        # ── 优先级 3：App 适配级（router_proxy __default__ / default_strategy_chain）/ 标准全链 ──
        decision = self._router.route_v2(
            element_name, entry,
            router_proxy_entry=None,
            default_proxy_entry=default_proxy_entry,
            is_native=is_native,
            app_default_chain=self._default_strategy_chain,
        )
        decision.strategies, skip_infos = self._build_capability_filtered_chain(
            decision.strategies, entry, is_native,
        )
        suffix = f"; {'; '.join(skip_infos)}" if skip_infos else ""
        chain_label = self._chain_label(decision.reason)
        logger.debug(
            "[Router] '%s' → %s (%s%s)",
            element_name,
            [s.value for s in decision.strategies],
            chain_label,
            suffix,
        )
        log_route_decision(
            element_name, decision.strategies, chain_label, decision.reason, skip_infos,
        )
        result = await self._try_chain(
            decision, entry, element_name,
            skip_l4=skip_l4, local_only=local_only, idx_offset=0,
        )
        if not result.found:
            logger.warning("[NOT_FOUND] could not resolve element: '%s'", element_name)
        return result

    def _entry_for_resolution(self, element_name: str):
        """获取元素上下文；`|` 多候选视觉元素按声明顺序取首个已登记项。"""
        entry = self._element_map.find(element_name)
        if entry is not None:
            return entry
        for candidate in self._split_visual_candidates(element_name):
            entry = self._element_map.find(candidate)
            if entry is not None:
                return entry
        return None

    @staticmethod
    def _db_route_configured(route: list[ResolveStrategy]) -> list[ResolveStrategy]:
        """DB 学习路径：L1 唯一注入；稳定/视觉策略前置，LLM 始终保留作末端兜底。"""
        preferred = [
            strategy for strategy in route
            if strategy not in (ResolveStrategy.MAP_EXACT, *Resolver._HTTP_STRATEGIES)
        ]
        return [ResolveStrategy.MAP_EXACT, *preferred]

    def _has_initial_visual_fingerprint(self, entry) -> bool:
        """当前平台 rosetta_map 的初始指纹存在且视觉解析器已装配。"""
        return bool(
            entry
            and getattr(entry, "patterns", None)
            and self._fingerprint_resolver is not None
        )

    def _build_capability_filtered_chain(
        self, configured: list[ResolveStrategy], entry, is_native: bool,
    ) -> tuple[list[ResolveStrategy], list[str]]:
        """构造标准补齐链并删除当前元素或运行时不可用的策略。

        Returns:
            (chain, skip_infos): chain 为可用策略列表；
            skip_infos 为本次跳过的信息片段（如视觉指纹未装配），
            由调用方决定是否打印（合并为单行日志）。
        """
        has_selector = bool(entry and entry.selector)
        has_visual_fp = self._has_initial_visual_fingerprint(entry)
        chain = build_effective_chain(
            list(dict.fromkeys(configured)), has_selector, is_native, has_pattern=has_visual_fp,
        )
        available: list[ResolveStrategy] = []
        skip_infos: list[str] = []
        for strategy in chain:
            if strategy is ResolveStrategy.QT_PROBE:
                # qt 通道为可选能力：引擎未装配探针、或该元素未声明 qt 字段时
                # 一律裁剪（不产生任何额外定位耗时）。见 docs/plans/
                # 20261116203324-win-qt-probe-channel-plan.md §4.5。
                if not getattr(self._engine, "has_qt_probe", False):
                    skip_infos.append("跳过<QT_PROBE>, 原因：qt 探针通道未装配")
                    continue
                if not getattr(entry, "qt_path", ""):
                    skip_infos.append("跳过<QT_PROBE>, 原因：该元素未声明 qt 路径")
                    continue
                available.append(strategy)
                continue
            if strategy is ResolveStrategy.VISUAL_FP and not has_visual_fp:
                skip_infos.append("跳过<VISUAL_FP>, 原因：该元素未装配视觉指纹")
                continue
            if strategy in self._HTTP_STRATEGIES and self._vision_client is None:
                skip_infos.append(f"跳过<{strategy.value}>, 原因：LLM 能力未装配")
                continue
            available.append(strategy)
        return available, skip_infos

    @staticmethod
    def _chain_label(reason: str) -> str:
        """将 route_v2 的 reason 字符串映射为日志中的链路标签。

        例: "标准全链" / "默认全链" / "定制全链"
        """
        if reason.startswith("router_proxy __default__ 命中") or reason.startswith("app default_strategy_chain 命中"):
            return "默认全链"
        if reason.startswith("router_proxy 定制命中") or reason.startswith("element strategy_chain 定制命中"):
            return "定制全链"
        return "标准全链"

    def _get_db_route(self, entry, *, source: str, is_native: bool) -> list[ResolveStrategy]:

        """读取有效 DB 路由；未知、无效或原生不支持的 token 均安全跳过。"""
        if entry is None or not entry.id or self._router_proxy_view is None:
            return []
        try:
            if True:
                return []
            if source == "default":
                raw_route = self._router_proxy_view.get_element_default_route(entry.id)
            else:
                raw_route = self._router_proxy_view.get_auto_heal_route(entry.id)
            if not isinstance(raw_route, str) or not raw_route.strip():
                return []
            route = parse_proxy_tokens([token.strip() for token in raw_route.split(",")])
            return [
                strategy for strategy in route
                if self._native_filter_strategy(strategy, is_native, entry.element_name, f"DB {source}")
                is not None
            ]
        except Exception as exc:
            logger.debug("[Resolver] DB %s 路由读取失败（忽略）: %s", source, exc)
            return []

    async def _try_resolve_real_selector(
        self,
        raw_selector: Optional[str],
        raw_locator: Optional[object],
        coords: Optional[tuple] = None,
    ) -> Optional[str]:
        """将语义描述符反查为真实 CSS selector，L2/L3/L4 统一入口。

        计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md

        路由：
          原生引擎（无 find_by_id_scoped）→ raw_selector 直接返回（AXPath 本身稳定）
          L4（coords 非 None）           → elementFromPoint 坐标反查
          L2/L3（text=/role= 开头）       → locator.evaluate 反查
          已是真实 CSS                    → raw_selector 直接返回
          反查失败/不唯一/不支持           → None（调用方跳过 selector 写入）
        """
        is_web = hasattr(self._engine, "find_by_id_scoped")
        if not is_web:
            # 原生引擎（Mac/Win）+ L4 坐标 + 引擎支持坐标反查（当前仅 MacAppKitEngine
            # 实现，见 docs/plans/archive/03-resolver-rosetta-实施纪要.md）
            # → 反查真实 AXPath/UIA selector；未实现（如 Windows 现状）或非坐标场景
            # → 原样返回 raw_selector（L1/L2/L3 场景本身就是真实 selector；Windows
            # L4 场景暂仍是占位符，与改造前行为一致，零回归）。
            if coords is not None and hasattr(self._engine, "resolve_native_selector_from_coords"):
                try:
                    real_selector = await self._engine.resolve_native_selector_from_coords(*coords)
                    if real_selector:
                        logger.info(
                            "[self-heal] L4 原生坐标反查成功: coords=%s → %r", coords, real_selector,
                        )
                    else:
                        logger.debug("[self-heal] L4 原生坐标反查无结果: coords=%s", coords)
                    return real_selector
                except Exception as exc:
                    logger.debug("[self-heal] L4 原生坐标反查异常: %s", exc)
                    return None
            return raw_selector

        # ── L4：坐标反查 ─────────────────────────────────────────────────
        if coords is not None and hasattr(self._engine, "resolve_css_selector_from_coords"):
            try:
                real_css = await self._engine.resolve_css_selector_from_coords(*coords)
                if real_css:
                    logger.info("[self-heal] L4 坐标反查成功: coords=%s → %r", coords, real_css)
                else:
                    logger.debug("[self-heal] L4 坐标反查无结果: coords=%s", coords)
                return real_css
            except Exception as exc:
                logger.debug("[self-heal] L4 坐标反查异常: %s", exc)
                return None

        # ── L2/L3：Locator 反查 ──────────────────────────────────────────
        if not raw_selector:
            return None
        # 非语义描述符（L1/L6 等已经是真实 CSS）→ 直接返回
        # "label=" 为 L4 LABEL_MATCH 语义描述符
        # docs/plans/20260803150000-label-match-playwright-get-by-label.md §3.5b）
        # ——不加入本判断会导致该字面量被误当真实 CSS 写回 selector 字段（Playwright
        # 无 "label" selector engine），破坏后续 L1 MAP_EXACT，必须走下方反查分支。
        if not raw_selector.startswith(("text=", "role=", "label=", "llm_visual:")):
            return raw_selector
        # 语义描述符 → 尝试反查
        if not hasattr(self._engine, "resolve_css_selector") or raw_locator is None:
            return None   # 引擎不支持或无 Locator → 不写垃圾
        try:
            real_css = await self._engine.resolve_css_selector(raw_locator)
            if real_css:
                logger.info("[self-heal] CSS 反查成功: %r → %r", raw_selector, real_css)
            else:
                logger.debug("[self-heal] CSS 反查无结果，跳过 selector 写入: %r", raw_selector)
            return real_css
        except Exception as exc:
            logger.debug("[self-heal] CSS 反查异常，跳过: %s", exc)
            return None

    async def _maybe_record_candidate(
        self, entry, decision, idx: int, strategy: ResolveStrategy,
        result: Optional["ResolveResult"] = None,
        latency_ms: float = 0.0,
    ) -> None:
        """非首选策略命中 + 当前无人工定制配置生效 + 有稳定 id → 记录候选 + 自愈回写。

        计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §4.2
        + docs/plans/archive/03-resolver-rosetta-实施纪要.md
        + docs/plans/archive/03-resolver-rosetta-实施纪要.md
        + docs/plans/archive/03-resolver-rosetta-实施纪要.md

        - idx == 0 跳过自愈记录：首选策略命中是"正常路径"，无信号价值。
          （方案 C 例外：idx==0 且策略为 MAP_EXACT 时，先触发 hit_count 清零——
          MAP_EXACT 命中代表 selector 有效，清除偶发降级累积，见计划文档问题 4）
        - decision.skip_self_heal 为真跳过：该元素已有人工定制配置在生效，不需要
          再"建议"一次配置，避免自我覆盖循环。
        - entry.id 为空跳过：router_proxy.json 以 id 为 key，无稳定 id 的元素无法
          安全定位。
        - decision 可为 None（第一轮精简链路无 route_v2 decision，默认 skip_self_heal=False）。
        """
        skip_self_heal = decision.skip_self_heal if decision else False
        if idx == 0:
            # 方案 C（计划文档问题 4）：MAP_EXACT 首选命中 → 清零 hit_count
            # （连续失败计数器语义），消除偶发降级历史累积，避免偶发被固化为
            # 稳定自愈策略。仅对 MAP_EXACT 生效（SEMANTIC 等其他首策略命中不代表
            # selector 有效）；skip_self_heal（人工配置生效）时不清零（人工配置
            # 不参与自愈学习）。
            if (
                strategy is ResolveStrategy.MAP_EXACT
                and not skip_self_heal
                and entry is not None and entry.id
            ):
                await self._reset_hit_count_safe(entry.id)
            return
        if skip_self_heal:
            return
        if entry is None or not entry.id:
            return

        # VISUAL_FP 必须来自当前平台 rosetta_map 的初始指纹；不允许 L4 或历史
        # 路由在无初始指纹时把它写入 fallback/default。
        if strategy is ResolveStrategy.VISUAL_FP and not self._has_initial_visual_fingerprint(entry):
            logger.debug("[self-heal] 跳过无初始视觉指纹的 VISUAL_FP 记录: element=%r", entry.element_name)
            return
        fallback_route = self._fallback_route_for_success(strategy, result)
        if not fallback_route:
            return

        # ── 延迟提交模式（HealBuffer 已注入）───────────────────────────
        if self._heal_buffer is not None:
            await self._buffer_heal_record(
                entry, strategy, result, latency_ms,
                fallback_route=fallback_route,
            )
            return

        # ── 立即写模式（heal_buffer=None）──────────────────────────────

        # 1. 候选记录（JSON）
        if self._router_proxy_candidate_store is not None:
            # 用 .name（如 "TEXT_MATCH"）而非 .value（如 "text_match"）——
            # 与 core/resolver/router.py::parse_proxy_tokens() 的 ResolveStrategy[tok]
            # 按枚举成员名反查保持一致，确保候选记录能直接被 promote 工具写入
            # router_proxy.json 后被正确解析。
            self._router_proxy_candidate_store.record_hit(
                entry.id, fallback_route, description=entry.element_name,
            )

        # 2. DB 策略路由记录（含真实 latency）+ 默认路由升级
        if self._router_proxy_view is not None:
            try:
                if False:
                    # 2a. 查询现有记录（策略锁定检查用）
                    existing_rec = self._router_proxy_view.get_record(entry.id)

                    # ★ 2a-bis. 策略锁定检查（2026-07-13 plan
                    #    docs/plans/archive/03-resolver-rosetta-实施纪要.md）
                    #    该元素已有记录 hit_count > LOCK_THRESHOLD → 已锁定，
                    #    拒绝写入不同策略（防止频繁切换）；同一策略仍可累加。
                    LOCK_THRESHOLD = 5
                    if (
                        existing_rec is not None
                        and existing_rec.hit_count > LOCK_THRESHOLD
                        and existing_rec.router_proxy != fallback_route
                    ):
                        logger.info(
                            "[self-heal] 策略已锁定 (hit_count>%d): element=%r "
                            "locked=%s → 跳过新策略 %s 写入",
                            LOCK_THRESHOLD, entry.element_name,
                            existing_rec.router_proxy, fallback_route,
                        )
                        return

                    if existing_rec is not None and existing_rec.router_proxy == fallback_route:
                        logger.info(
                            "[self-heal] router_proxy 已有记录: element=%r route=%s "
                            "hit_count=%d → 累加更新",
                            entry.element_name, fallback_route, existing_rec.hit_count,
                        )
                    else:
                        logger.info(
                            "[self-heal] router_proxy 无记录: element=%r route=%s → 新建",
                            entry.element_name, fallback_route,
                        )
                    # 2b. upsert（有则累加，无则新建）
                    self._router_proxy_view.record_hit(
                        entry.id, fallback_route, source="fallback", latency_ms=latency_ms,
                    )
                    logger.info(
                        "[self-heal] router_proxy 回写完成: element_id=%s route=%s latency=%.0fms",
                        entry.id, fallback_route, latency_ms,
                    )
                    # 2c. 写入默认策略路由（source=fallback，hit_count >= 3 时升级 default）
                    _self_heal_promote_if_ready(
                        entry.id, fallback_route, self._router_proxy_view, entry.element_name,
                    )
            except Exception as exc:
                logger.debug("[DB] router_proxy record_hit 失败，回退 JSON: %s", exc)

        # 3. 自愈元素属性回写（selector/text/role → DB fallback 表）
        await self._self_heal_writeback(entry, strategy, result)

    @staticmethod
    def _fallback_route_for_success(
        strategy: ResolveStrategy, result: Optional["ResolveResult"],
    ) -> str:
        """把本次实际成功路径编码成下次可复用的 DB fallback 路由。"""
        if strategy in (ResolveStrategy.LLM_SELECTOR, ResolveStrategy.LLM_VISUAL):
            stable = result.fallback_strategy if result else ""
            # MAP_EXACT 的稳定 selector 已通过属性回写在 L1 生效，不需要在
            # 路由中重复；LLM 仍保留为无法复用时的末端兜底。
            if stable and stable != ResolveStrategy.MAP_EXACT.name:
                return f"{stable},{strategy.name}"
            return strategy.name
        return strategy.name

    @staticmethod
    def _bounded_llm_prompt(prompt: str) -> str:
        """限制持久化 prompt 大小，避免审计字段吞入异常大请求。"""
        return (prompt or "").strip()[:4096]

    async def _buffer_heal_record(
        self, entry, strategy: ResolveStrategy,
        result: Optional["ResolveResult"], latency_ms: float, *, fallback_route: str,
    ) -> None:
        """将自愈信息封装为 HealRecord 放入 heal_buffer（延迟提交模式）。

        Phase 3 新增 CSS 反查：L2/L3/L4 兜底命中时异步反查真实 CSS selector，
        反查成功写 selector 字段；失败则 selector=None（不写垃圾数据）。
        计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md
        """
        from uita.core.runner.heal_buffer import HealRecord

        selector: Optional[str] = None
        selector_field = "mac"
        if result is not None and result.element is not None:
            is_web = hasattr(self._engine, "find_by_id_scoped")
            if is_web:
                selector_field = "selector"
            else:
                platform = getattr(self._engine, "platform", "").lower()
                selector_field = "win" if "win" in platform else "mac"

            if result.level is ResolveLevel.L7_VISUAL_FP:
                # 纯本机 OpenCV 坐标，无 Playwright Page 上下文，跳过 selector 回写
                selector = None
            else:
                # L1~L6/L8：统一走反查逻辑
                coords = result.element.coords if result.level is ResolveLevel.L8_LLM_VISUAL else None
                selector = await self._try_resolve_real_selector(
                    raw_selector=result.element.element_id or None,
                    raw_locator=result.element.raw,
                    coords=coords,
                )

        self._heal_buffer.record(HealRecord(
            element_id=entry.id,
            strategy_name=fallback_route,
            latency_ms=latency_ms,
            description=entry.element_name,
            selector=selector,
            selector_field=selector_field,
            heal_text=result.heal_text if result else "",
            heal_role=result.heal_role if result else "",
            heal_role_name=result.heal_role_name if result else "",
            heal_label=result.heal_label if result else "",
            fallback_route=fallback_route,
            llm_prompt=self._bounded_llm_prompt(result.heal_llm_prompt) if result else "",
        ))

    # ── 双轮降级辅助方法 ──────────────────────────────────────────────

    # 原生引擎不适用的自愈策略（与 build_effective_chain 的 is_native 裁剪一致，
    # 当前仅TEXT_MATCH：AX/UIA 树遍历成本高。见计划文档问题 3）。
    _NATIVE_UNSUPPORTED_STRATEGIES = frozenset({ResolveStrategy.TEXT_MATCH})

    def _native_filter_strategy(
        self, strategy: ResolveStrategy, is_native: bool, element_name: str, tag: str,
    ) -> Optional[ResolveStrategy]:
        """原生引擎策略裁剪（计划文档问题 3）：原生引擎下不适用的策略返回 None。

        与 build_effective_chain() 的 is_native 裁剪规则保持一致，避免把
        TEXT_MATCH 等不适用策略塞进 first_chain 第二位后又被 build_effective_chain
        裁掉的无效逻辑（且日志显示误导性auto_heal 策略）。
        """
        if is_native and strategy in self._NATIVE_UNSUPPORTED_STRATEGIES:
            logger.debug(
                "[Resolver] %s策略 %s 不适用原生引擎，忽略: element=%r",
                tag, strategy.value, element_name,
            )
            return None
        return strategy

    def _get_auto_heal_strategy(
        self, entry, is_native: bool = False,
    ) -> Optional[ResolveStrategy]:
        """从 DB（{app}_router_proxy 表，source=fallback）读取自愈策略（hit_count >= 2）。

        计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §4.5
        三表迁移：docs/plans/archive/03-resolver-rosetta-实施纪要.md
        原生引擎裁剪：docs/plans/archive/03-resolver-rosetta-实施纪要.md 问题 3

        无 router_proxy_view / DB 不可用 / 无记录 / 原生引擎不适用策略 → 返回 None
        （走标准全链，零回归）。
        """
        if entry is None or not entry.id:
            return None
        if self._router_proxy_view is None:
            return None
        try:
            if True:
                logger.debug("[Resolver] DB 不可用，跳过自愈策略查询: %r", entry.element_name)
                return None

            best = self._router_proxy_view.get_auto_heal_route(entry.id)
            if best:
                first = best.split(",")[0].strip()
                if not first:
                    logger.debug("[Resolver] 策略链首项为空，忽略: %r", best)
                else:
                    try:
                        strategy = ResolveStrategy[first]
                        # 原生引擎裁剪（问题 3）：不适用则视为无自愈策略
                        if self._native_filter_strategy(
                            strategy, is_native, entry.element_name, "自愈",
                        ) is None:
                            return None
                        logger.info(
                            "[Resolver] 自愈策略: element=%r id=%s route=%s → 首策略=%s",
                            entry.element_name, entry.id, best, first,
                        )
                        return strategy
                    except KeyError:
                        logger.debug("[Resolver] 未知策略名 %r，忽略", first)
            else:
                logger.debug(
                    "[Resolver] DB 无自愈策略（hit_count < 2）: element=%r id=%s → 走标准全链",
                    entry.element_name, entry.id,
                )
        except Exception as exc:
            logger.debug("[auto_heal] get_auto_heal_strategy 失败（忽略）: %s", exc)
        return None

    def _get_element_default_strategy(
        self, entry, is_native: bool = False,
    ) -> Optional[ResolveStrategy]:
        """从 DB 读取元素专属 default 策略（source=default，promote 转正策略）。

        计划文档问题 2：promote 后 source=fallback→default，该记录需被优先级 1.5
        消费（否则悬空）。与 _get_auto_heal_strategy() 对称，但查
        get_element_default_route()（source=default），同样做原生引擎裁剪。

        无 view / DB 不可用 / 无记录 / 原生引擎不适用 → None。
        """
        if entry is None or not entry.id:
            return None
        if self._router_proxy_view is None:
            return None
        try:
            if True:
                return None
            best = self._router_proxy_view.get_element_default_route(entry.id)
            if not best:
                return None
            first = best.split(",")[0].strip()
            if not first:
                return None
            try:
                strategy = ResolveStrategy[first]
            except KeyError:
                logger.debug("[Resolver] 元素 default 未知策略名 %r，忽略", first)
                return None
            if self._native_filter_strategy(
                strategy, is_native, entry.element_name, "元素default",
            ) is None:
                return None
            logger.info(
                "[Resolver] 元素专属 default 策略: element=%r id=%s route=%s → 首策略=%s",
                entry.element_name, entry.id, best, first,
            )
            return strategy
        except Exception as exc:
            logger.debug("[element_default] get_element_default_strategy 失败（忽略）: %s", exc)
            return None

    async def _reset_hit_count_safe(self, element_id: str) -> None:
        """MAP_EXACT 命中清零 hit_count（方案 C，计划文档问题 4）。

        heal_buffer 非 None → 走 buffer.record_reset（延迟到用例成功后提交，
        与 record 双模式对称）；否则立即 reset_hit_count（DB 直写）。异常静默。
        """
        try:
            if self._heal_buffer is not None:
                self._heal_buffer.record_reset(element_id)
                return
            if self._router_proxy_view is None:
                return
            if True:
                return
            self._router_proxy_view.reset_hit_count(element_id)
        except Exception as exc:
            logger.debug("[reset_hit_count] 清零失败（忽略）: %s", exc)

    # local_only=True 时需跳过的 HTTP/远程策略集合（LLM_SELECTOR 需引擎摘要+LLM HTTP，
    # LLM_VISUAL 需截图+VL HTTP，单次调用真机实测 ~3s；探测"元素不存在"场景纯属浪费）
    _HTTP_STRATEGIES = frozenset({ResolveStrategy.LLM_SELECTOR, ResolveStrategy.LLM_VISUAL})

    def _filter_skipped_strategies(
        self, strategies, *, skip_l4: bool, local_only: bool, element_name: str,
    ) -> list:
        """预过滤 skip_l4/local_only 命中的策略，合并成单条日志输出，避免逐条
        重复打印同一元素的 N 条几乎一样的 skip 日志（见用户反馈 2026-08-12）。
        返回保留下来待尝试的 (原始索引, strategy) 列表。
        """
        active: list = []
        skipped_l4: list = []
        skipped_http: list = []
        for i, strategy in enumerate(strategies):
            if skip_l4 and strategy is ResolveStrategy.LLM_VISUAL:
                skipped_l4.append(strategy.value)
                continue
            if local_only and strategy in self._HTTP_STRATEGIES:
                skipped_http.append(strategy.value)
                continue
            active.append((i, strategy))
        if skipped_l4:
            logger.debug("[Resolver] skip_l4=True，跳过 %s: '%s'", skipped_l4, element_name)
        if skipped_http:
            logger.debug("[Resolver] local_only=True，跳过 HTTP 策略 %s: '%s'", skipped_http, element_name)
        return active

    async def _try_chain(
        self, decision, entry, element_name: str, *,
        skip_l4: bool, local_only: bool = False, idx_offset: int,
    ) -> ResolveResult:
        """遍历 decision.strategies 尝试，命中后记录候选 + 回写。"""
        active = self._filter_skipped_strategies(
            decision.strategies, skip_l4=skip_l4, local_only=local_only, element_name=element_name,
        )
        for i, strategy in active:
            idx = idx_offset + i
            t0 = time.monotonic()
            result = await self._try_strategy_v2(strategy, element_name)
            latency_ms = (time.monotonic() - t0) * 1000
            log_locate_attempt(element_name, strategy, result, latency_ms, step_idx=idx)
            if result.found:
                await self._maybe_record_candidate(
                    entry, decision, idx, strategy, result, latency_ms=latency_ms,
                )
                return result
        return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

    async def _try_chain_strategies(
        self, strategies, entry, element_name: str, *,
        skip_l4: bool, local_only: bool = False, idx_offset: int,
    ) -> ResolveResult:
        """直接传入策略列表（不经过 route_v2），用于第一轮精简链路。"""
        active = self._filter_skipped_strategies(
            strategies, skip_l4=skip_l4, local_only=local_only, element_name=element_name,
        )
        for i, strategy in active:
            idx = idx_offset + i
            t0 = time.monotonic()
            result = await self._try_strategy_v2(strategy, element_name)
            latency_ms = (time.monotonic() - t0) * 1000
            log_locate_attempt(element_name, strategy, result, latency_ms, step_idx=idx)
            if result.found:
                await self._maybe_record_candidate(
                    entry, None, idx, strategy, result, latency_ms=latency_ms,
                )
                return result
        return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

    async def _self_heal_writeback(
        self, entry, strategy: ResolveStrategy, result: Optional["ResolveResult"],
    ) -> None:
        """自愈成功后回写元素属性到 DB（立即写模式）。

        计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md
        Phase 3 CSS 反查：docs/plans/archive/03-resolver-rosetta-实施纪要.md
        三表迁移：docs/plans/archive/03-resolver-rosetta-实施纪要.md

        写入 {app}_rosetta_map_fallback 表（唯一回写路径）。

        selector 获取逻辑：
          L7   → 跳过（无 Playwright Page 上下文）
          L8   → elementFromPoint 坐标反查 → 写 selector 字段
          L3/L4/L5 语义描述符 → locator.evaluate 反查 → 写 selector 字段
          其余 → element_id 本身即真实 CSS/AXPath，直接用
          反查失败 → selector=None，仍写 text/role（不跳过）
        """
        # 入口守卫
        if result is None or result.element is None:
            return
        if result.level is ResolveLevel.L7_VISUAL_FP:
            return   # 纯本机 OpenCV 坐标，无 Playwright Page，无法反查
        if self._fallback_view is None:
            return

        is_web = hasattr(self._engine, "find_by_id_scoped")
        if is_web:
            field = "selector"
        else:
            platform = getattr(self._engine, "platform", "").lower()
            if "win" in platform:
                field = "win"
            elif "linux" in platform:
                field = "linux"
            else:
                field = "mac"

        # ── CSS selector 反查（Phase 3）────────────────────────────────
        coords = result.element.coords if result.level is ResolveLevel.L8_LLM_VISUAL else None
        selector = await self._try_resolve_real_selector(
            raw_selector=result.element.element_id or None,
            raw_locator=result.element.raw,
            coords=coords,
        )
        # selector 为 None 时不跳过（text/role/role_name 仍可回写）

        try:
            if True:
                logger.debug("[self-heal] DB 探活失败，跳过元素属性回写: %r", entry.element_name)
                return

            self._fallback_view.self_heal_writeback(
                entry.id,
                selector=selector, selector_field=field,
                text=result.heal_text or None,
                role=result.heal_role or None,
                role_name=result.heal_role_name or None,
                label=result.heal_label or None,
                llm_prompt=self._bounded_llm_prompt(result.heal_llm_prompt) or None,
            )
            self._fallback_view.record_success(entry.id)
            logger.info(
                "[self-heal] %s 回写成功: %r → selector=%r (field=%s)",
                strategy.name, entry.element_name, selector, field,
            )
        except Exception as exc:
            logger.debug("[self-heal] 元素属性回写失败（非致命）: %s", exc)

    async def _try_strategy(self, strategy: ResolveStrategy, element_name: str) -> ResolveResult:
        """按 ActionRouter 决策的单个策略执行定位。"""
        if strategy is ResolveStrategy.MAP_EXACT:
            result = await self._l1_map_exact(element_name)
            if result.found:
                logger.debug("[L1] resolved '%s' → %s", element_name, result.element.element_id)
                return result
            return await self._l2_semantic_match(element_name)

        if strategy is ResolveStrategy.TEXT_MATCH:
            result = await self._l3_text_match(element_name)
            if result.found:
                logger.debug("[L3] resolved '%s' via text match", element_name)
            return result

        if strategy is ResolveStrategy.LABEL_MATCH:
            return await self._l4_label_match(element_name)

        if strategy is ResolveStrategy.ROLE_MATCH:
            return await self._l5_role_match(element_name)

        if strategy is ResolveStrategy.VISUAL_FP:
            return await self._l7_visual_fingerprint(element_name)

        if strategy is ResolveStrategy.LLM_SELECTOR:
            return await self._l6_llm_selector(element_name)

        if strategy is ResolveStrategy.LLM_VISUAL:
            return await self._l8_llm_visual(element_name)

        return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

    async def _try_strategy_v2(self, strategy: ResolveStrategy, element_name: str) -> ResolveResult:
        """route_v2() 专用的单策略执行。

        与 _try_strategy() 的唯一差异：MAP_EXACT 只做纯 L1 精确匹配，不再自动
        内链 L2 语义匹配（因为 SEMANTIC 是链路里独立的一步，避免
        [MAP_EXACT, SEMANTIC, ...] 相邻时产生冗余调用）。其余策略分支
        直接委托 _try_strategy()，逐字节复用现有实现。
        """
        if strategy is ResolveStrategy.MAP_EXACT:
            result = await self._l1_map_exact(element_name)
            if result.found:
                logger.debug("[L1] resolved '%s' → %s", element_name, result.element.element_id)
            return result

        if strategy is ResolveStrategy.SEMANTIC:
            return await self._l2_semantic_match(element_name)

        if strategy is ResolveStrategy.QT_PROBE:
            return await self._l9_qt_probe(element_name)

        return await self._try_strategy(strategy, element_name)

    # ── 声明式首策略直达（供 click/type/hover 的 selector=/role=/text= kwarg 使用）──

    async def resolve_with_strategy(self, element_name: str, declared: dict) -> ResolveResult:
        """首策略直达 + 剩余降级。"""
        try:
            if "selector" in declared:
                element = await self._engine.find_by_id(declared["selector"])
                if element is not None:
                    logger.debug("[declared] resolved '%s' via selector=%r", element_name, declared["selector"])
                    return ResolveResult(element=element, level=ResolveLevel.L1_MAP_EXACT, element_name=element_name)
            elif "role" in declared:
                element = await self._engine.find_by_role(declared["role"], name=declared.get("name", ""))
                if element is not None:
                    logger.debug("[declared] resolved '%s' via role=%r", element_name, declared["role"])
                    return ResolveResult(element=element, level=ResolveLevel.L5_ROLE_MATCH, element_name=element_name)
            elif "label" in declared:
                element = await self._engine.find_by_label(declared["label"])
                if element is not None:
                    logger.debug("[declared] resolved '%s' via label=%r", element_name, declared["label"])
                    return ResolveResult(
                        element=element, level=ResolveLevel.L4_LABEL_MATCH,
                        element_name=element_name, heal_label=declared["label"],
                    )
            elif "text" in declared:
                element = await self._engine.find_by_text(declared["text"])
                if element is not None:
                    logger.debug("[declared] resolved '%s' via text=%r", element_name, declared["text"])
                    return ResolveResult(element=element, level=ResolveLevel.L3_TEXT_MATCH, element_name=element_name)
        except Exception as exc:
            logger.debug("[declared] 首策略异常（忽略，降级到标准 resolve）: %s", exc)

        if self._repair_mode:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        return await self.resolve(element_name)

    # ── L1：精确映射 ──────────────────────────────────────────────────

    async def resolve_exact(self, element_name: str) -> ResolveResult:
        """仅 L1 精确定位（引擎原生控件树/DOM 直查），**不降级、不截图、不调 LLM**。

        供性能测量的高频"元素是否已出现"轮询使用：未命中即 NOT_FOUND，不会像
        :meth:`resolve` 那样继续走 L2/L3/视觉指纹等耗时链路。
        """
        return await self._l1_map_exact(element_name)

    async def _l1_map_exact(self, element_name: str) -> ResolveResult:
        entry = self._element_map.find(element_name)
        if entry is None:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        selector = self._substitute_case_variables(self._get_effective_selector(entry))
        async with self._window_scope(entry):
            element = await self._engine.find_by_id(selector)
        if element is None:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        return ResolveResult(element=element, level=ResolveLevel.L1_MAP_EXACT, element_name=element_name)

    def _window_scope(self, entry):
        """元素声明了 `window_hint` 且引擎提供 `window_scope` 能力时，返回该窗口作用域
        异步上下文；否则返回空上下文，调用行为与引入该字段前逐字节一致
        （mac/web 引擎无此能力，未声明 hint 的元素亦无作用域）。
        """
        hint = getattr(entry, "window_hint", None) if entry is not None else None
        scope_factory = getattr(self._engine, "window_scope", None)
        if isinstance(hint, WindowHint) and callable(scope_factory):
            return scope_factory(hint)
        return _NullAsyncScope()

    async def _l9_qt_probe(self, element_name: str) -> ResolveResult:
        """L9：QtProbe Qt 探针通道定位（进程内对象树，替代 UIA 树遍历）。"""
        entry = self._element_map.find(element_name)
        qt_path = getattr(entry, "qt_path", "") if entry is not None else ""
        if not qt_path:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        finder = getattr(self._engine, "find_by_qt_path", None)
        if not callable(finder):
            logger.debug("[L9] 引擎无 find_by_qt_path 能力，跳过 '%s'", element_name)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        element = await finder(self._substitute_case_variables(qt_path))
        if element is None:
            logger.debug("[L9] qt 通道未命中 '%s' → %r", element_name, qt_path)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        logger.debug("[L9] resolved '%s' → %s", element_name, element.element_id)
        return ResolveResult(
            element=element, level=ResolveLevel.L9_QT_PROBE, element_name=element_name,
        )

    @staticmethod
    def _substitute_case_variables(selector: str) -> str:
        """Expand TCS ${{VAR}} tokens used by dynamic rosetta selectors."""
        if not isinstance(selector, str):
            return selector

        def replace(match: re.Match) -> str:
            name = match.group(1)
            value = settings.CASE_VARIABLES.get(name)
            return match.group(0) if value is None else str(value)

        return re.sub(r"\$\{\{([A-Za-z_]\w*)\}\}", replace, selector)

    def _get_effective_selector(self, entry) -> str:
        """获取有效 selector：优先 DB fallback 表自愈回写值，回退 FlatEntry.selector 原始值。

        计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md
        三表迁移：docs/plans/archive/03-resolver-rosetta-实施纪要.md

        DB 不可用 / 无记录 / 无 View → 回退 entry.selector（零回归）。
        """
        if self._repair_mode or not entry or not entry.id:
            return entry.selector

        try:
            if True:
                return entry.selector

            if self._fallback_view is not None:
                fb = self._fallback_view.find_by_id(entry.id)
                if fb is not None and fb.effective_selector:
                    return fb.effective_selector

        except Exception as exc:
            logger.debug("[L1] selector 查询失败（回退原始）: %s", exc)

        return entry.selector

    # ── L2：语义近似匹配 ────────────────────────────────────────────

    async def _l2_semantic_match(self, element_name: str) -> ResolveResult:
        from uita.core.platform.semantic_matcher import find_candidates
        from uita.core.resolver.router_policy import RouterPolicy

        candidates = find_candidates(
            element_name,
            self._element_map.all_entries(),
            page_context=self._page_context,
            threshold=_get_semantic_threshold(),
        )
        if not candidates:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        # 界面核对：count==1 才命中；count==0（不存在）或 count>1（歧义）均跳过
        for candidate in candidates:
            async with self._window_scope(candidate):
                count = await self._engine.count_by_id(candidate.selector)
                element = (
                    await self._engine.find_by_id(candidate.selector) if count == 1 else None
                )
            if count == 1:
                if element is not None:
                    logger.debug(
                        "[L2] resolved '%s' → '%s' via semantic (page_ctx=%r)",
                        element_name, candidate.element_name, self._page_context,
                    )
                    return ResolveResult(
                        element=element,
                        level=ResolveLevel.L2_SEMANTIC,
                        element_name=element_name,
                    )
            elif count > 1:
                logger.debug(
                    "[L2] selector '%s' matched %d elements (ambiguous), skip",
                    candidate.selector, count,
                )

        # 所有候选核对失败 → RouterPolicy 降级
        policy = RouterPolicy(strategy_store=getattr(self, "_strategy_store", None))
        fallback_entry = await policy.fallback(
            element_name, candidates, self._engine, self._page_context
        )
        if fallback_entry is not None:
            async with self._window_scope(fallback_entry):
                count = await self._engine.count_by_id(fallback_entry.selector)
                element = (
                    await self._engine.find_by_id(fallback_entry.selector) if count == 1 else None
                )
            if count == 1:
                if element is not None:
                    logger.debug("[L2] router_policy fallback resolved '%s'", element_name)
                    return ResolveResult(
                        element=element,
                        level=ResolveLevel.L2_SEMANTIC,
                        element_name=element_name,
                    )

        return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

    # ── L3：文本匹配 ──────────────────────────────────────────────────

    async def _l3_text_match(self, element_name: str) -> ResolveResult:
        search_text = element_name
        try:
            entry = self._element_map.find(element_name)
            if entry and entry.selector:
                import re
                m = re.search(r'(?:AXStaticText|AXButton)\[value=([^\]]+)\]', entry.selector)
                if m:
                    search_text = m.group(1)
        except Exception:
            pass

        element = await self._engine.find_by_text(search_text)
        if element is None:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        return ResolveResult(
            element=element, level=ResolveLevel.L3_TEXT_MATCH,
            element_name=element_name, heal_text=search_text,
        )

    # ── L4：关联 <label> 文本匹配 ──────────────────────────────────────

    async def _l4_label_match(self, element_name: str) -> ResolveResult:
        """通过关联 <label> 文本定位元素。"""
        entry = self._element_map.find(element_name)
        if entry and entry.label:
            element = await self._engine.find_by_label(entry.label)
            if element is not None:
                logger.debug("[L4] resolved '%s' via label='%s' (rosetta_map)", element_name, entry.label)
                return ResolveResult(
                    element=element, level=ResolveLevel.L4_LABEL_MATCH,
                    element_name=element_name, heal_label=entry.label,
                )

        element = await self._engine.find_by_label(element_name)
        if element is not None:
            logger.debug("[L4] resolved '%s' via label=<element_name> (兜底)", element_name)
            return ResolveResult(
                element=element, level=ResolveLevel.L4_LABEL_MATCH,
                element_name=element_name, heal_label=element_name,
            )

        return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

    # ── L5：角色匹配 ──────────────────────────────────────────────────

    async def _l5_role_match(self, element_name: str) -> ResolveResult:
        """按角色定位元素。"""
        from uita.core.resolver.router import infer_role_from_description

        entry = self._element_map.find(element_name)
        explicit_role = entry.role if entry else ""
        role = explicit_role or infer_role_from_description(element_name)
        if not role:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        role_name_hint = entry.role_name if (entry and explicit_role) else ""

        try:
            if role_name_hint:
                element = await self._engine.find_by_role(role, name=role_name_hint)
                if element is not None:
                    logger.debug("[L5] resolved '%s' via role='%s', name='%s'", element_name, role, role_name_hint)
                    return ResolveResult(
                        element=element, level=ResolveLevel.L5_ROLE_MATCH,
                        element_name=element_name,
                        heal_role=role, heal_role_name=role_name_hint,
                    )
                return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

            candidates = await self._engine.find_all_by_role(role)
            if len(candidates) == 1:
                logger.debug("[L5] resolved '%s' via inferred role='%s' (unique match)", element_name, role)
                return ResolveResult(
                    element=candidates[0], level=ResolveLevel.L5_ROLE_MATCH,
                    element_name=element_name,
                    heal_role=role,
                )
            if len(candidates) > 1:
                logger.debug("[L5] role='%s' matched %d elements (ambiguous), skip", role, len(candidates))
        except Exception as exc:
            logger.debug("[L5] role_match 异常（忽略）: %s", exc)

        return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

    # ── L7：视觉指纹匹配 ──────────────────────────────────────────────

    async def resolve_visual(
        self, element_name: str, match_threshold: Optional[float] = None,
    ) -> ResolveResult:
        """按调用级置信度执行 rosetta_map 初始视觉指纹匹配。"""
        entry = self._entry_for_resolution(element_name)
        if not self._has_initial_visual_fingerprint(entry):
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        return await self._l7_visual_fingerprint(
            element_name, match_threshold=match_threshold,
        )

    def has_initial_visual_fingerprint(self, element_name: str) -> bool:
        """公开判定：该元素在当前平台是否配了可用的视觉指纹。

        多候选（`|` 分隔）任一可用即 True。供上层区分"指纹没配"与"指纹没命中"
        ——两者对调用方的处置完全不同（前者应立刻跳过，后者应继续等/重试）。
        """
        for candidate in self._split_visual_candidates(element_name):
            if self._has_initial_visual_fingerprint(self._element_map.find(candidate)):
                return True
        return False

    async def visual_present(
        self, element_name: str, match_threshold: Optional[float] = None,
    ) -> Optional[bool]:
        """高频轮询用：直接在被测窗口的**内存画面**上判指纹是否出现。

        与 :meth:`resolve_visual` 同一套模板/阈值/``click_rect`` 匹配，区别是省掉与"是否出现"
        无关的开销：不经 PNG 编码/落盘/回读解码，也不构造坐标换算器（不需要点击坐标）。

        Returns:
            ``True``/``False`` = 已判定；``None`` = 本路径不可用（引擎无 ``grab_bgr``、
            抓取失败、无指纹解析器），调用方应退回 :meth:`resolve_visual`。
        """
        grab = getattr(self._engine, "grab_bgr", None)
        if self._fingerprint_resolver is None or not callable(grab):
            return None
        candidates = [
            candidate for candidate in self._split_visual_candidates(element_name)
            if self._has_initial_visual_fingerprint(self._element_map.find(candidate))
        ]
        if not candidates:
            return False
        try:
            frame = await grab()
        except Exception as exc:  # noqa: BLE001 - 抓取失败交给调用方退回截图路径
            logger.debug("[L7] grab_bgr 异常（退回截图路径）: %s", exc)
            return None
        if frame is None:
            return None
        for candidate in candidates:
            entry = self._element_map.find(candidate)
            match_kwargs: dict = {"click_rect": entry.click_rect if entry else None}
            if match_threshold is not None:
                match_kwargs["match_threshold"] = match_threshold
            if self._fingerprint_resolver.match(candidate, frame, **match_kwargs) is not None:
                return True
        return False

    async def visual_match_detail(
        self, element_name: str, match_threshold: Optional[float] = None,
    ) -> dict:
        """拍摄当前画面并按 L7 匹配，返回命中详情（不抛异常）。

        Returns:
            ``{"template": 文件名, "confidence": float, "algo": str, "bbox": (x,y,w,h)}``；
            未命中/无指纹/解码失败时返回 ``{}``。

        Note:
            仅用于报告展示"5–10 张指纹里命中哪张"，坐标未做全屏降级裁剪，
            因此**不要**拿它当点击坐标（点击仍走 :meth:`resolve_visual`）。
        """
        if self._fingerprint_resolver is None:
            return {}
        candidates = [
            candidate for candidate in self._split_visual_candidates(element_name)
            if self._has_initial_visual_fingerprint(self._element_map.find(candidate))
        ]
        if not candidates:
            return {}
        try:
            screenshot = await self._engine.take_screenshot()
            if screenshot is None:
                return {}
            screenshot_bgr = self._decode_screenshot_to_bgr(screenshot)
            if screenshot_bgr is None:
                return {}
            for candidate in candidates:
                match = self._fingerprint_resolver.match(
                    candidate, screenshot_bgr, match_threshold=match_threshold,
                )
                if match is None:
                    continue
                return {
                    "template": getattr(match, "template_path", "") or "",
                    "confidence": float(getattr(match, "confidence", 0.0) or 0.0),
                    "algo": str(getattr(match, "match_algo", "") or ""),
                    "bbox": (
                        int(getattr(match, "x", 0)),
                        int(getattr(match, "y", 0)),
                        int(getattr(match, "width", 0)),
                        int(getattr(match, "height", 0)),
                    ),
                }
        except Exception as exc:  # noqa: BLE001 - 详情失败不影响命中判定
            logger.debug("[L7] visual_match_detail 失败（忽略）: %s", exc)
            return {}
        return {}

    @staticmethod
    def _split_visual_candidates(element_name: str) -> list[str]:
        """拆解 `|` 分隔的多候选视觉元素名（对齐 rosetta_map 多语言候选的
        "按声明顺序依次尝试，首个命中即用"语义，见宪法 R22c）。"""
        parts = [part.strip() for part in str(element_name).split("|")]
        return parts or [""]

    def match_visual_snapshot(
        self, element_name: str, screenshot: bytes, match_threshold: Optional[float] = None,
    ) -> bool:
        """在指定截图字节上断言视觉指纹，不重新截图。多候选任一命中即 True。"""
        if self._fingerprint_resolver is None:
            return False
        candidates = self._split_visual_candidates(element_name)
        if not any(self._has_initial_visual_fingerprint(self._element_map.find(candidate)) for candidate in candidates):
            return False
        screenshot_bgr = self._decode_screenshot_to_bgr(screenshot)
        if screenshot_bgr is None:
            return False
        for candidate in candidates:
            if not self._has_initial_visual_fingerprint(self._element_map.find(candidate)):
                continue
            match = self._fingerprint_resolver.match(
                candidate,
                screenshot_bgr,
                match_threshold=match_threshold,
                color_sensitive=True,
            )
            if match is not None:
                return True
        return False

    async def _l7_visual_fingerprint(
        self, element_name: str, match_threshold: Optional[float] = None,
    ) -> ResolveResult:
        """OpenCV 模板匹配定位（core/resolver/l7_visual_fingerprint.py）。"""
        if self._fingerprint_resolver is None:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        candidates = self._split_visual_candidates(element_name)
        if not any(self._has_initial_visual_fingerprint(self._element_map.find(candidate)) for candidate in candidates):
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        crop_off_x, crop_off_y = 0, 0
        try:
            screenshot = await self._engine.take_screenshot()
            if screenshot is None:
                return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
            screenshot_bgr = self._decode_screenshot_to_bgr(screenshot)
            if screenshot_bgr is None:
                return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
            converter = await self._build_screen_converter(screenshot)
            search_img, crop_off_x, crop_off_y = self._crop_fullscreen_to_window(
                screenshot_bgr, converter,
            )
            for candidate in self._split_visual_candidates(element_name):
                entry = self._element_map.find(candidate)
                if not self._has_initial_visual_fingerprint(entry):
                    logger.debug("[L7] candidate '%s' 无当前平台初始视觉指纹，跳过", candidate)
                    continue
                click_rect = entry.click_rect if entry else None
                match_kwargs = {"click_rect": click_rect}
                if match_threshold is not None:
                    match_kwargs["match_threshold"] = match_threshold
                match = self._fingerprint_resolver.match(
                    candidate, search_img, **match_kwargs,
                )
                if match is None:
                    logger.debug("[L7] candidate '%s' not matched", candidate)
                    continue
                click_cx, click_cy = match.click_center
                screen_center = converter.to_screen(
                    click_cx + crop_off_x, click_cy + crop_off_y,
                )
                if screen_center is None:
                    logger.debug(
                        "[L7] 坐标换算失败（截图解码/阴影降级场景等），降级 NOT_FOUND: %r",
                        candidate,
                    )
                    continue
                screen_bbox = converter.to_screen_bbox(
                    match.x + crop_off_x, match.y + crop_off_y,
                    match.width, match.height,
                )
                logger.debug(
                    "[L7] resolved '%s' via visual fingerprint at %s (confidence=%.3f, algo=%s, click_rect=%s)",
                    candidate, screen_center, match.confidence, match.match_algo,
                    bool(match.sub_bbox),
                )
                element = ElementRef(
                    element_id=f"visual_fp:{candidate}", raw=None, coords=screen_center,
                    bbox=screen_bbox,
                )
                return ResolveResult(
                    element=element, level=ResolveLevel.L7_VISUAL_FP, element_name=candidate,
                )
        except Exception as exc:
            logger.debug("[L7] visual_fingerprint 异常（忽略）: %s", exc)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

    async def _build_screen_converter(self, screenshot_bytes: bytes) -> "ScreenCoordConverter":
        """构造本次截图的 ScreenCoordConverter（L7/L8 复用）。"""
        from uita.core.resolver.screen_coords import ScreenCoordConverter

        window_rect = None
        try:
            window_rect = await self._engine.get_window_rect()
        except Exception as exc:
            logger.debug("[ScreenCoord] get_window_rect 异常（忽略）: %s", exc)

        dpr_hint = None
        try:
            dpr_hint = float(await self._engine.evaluate("() => window.devicePixelRatio"))
        except Exception:
            dpr_hint = None

        img_w, img_h, has_alpha = self._screenshot_image_info(screenshot_bytes) or (0, 0, False)

        return ScreenCoordConverter(
            is_window_screenshot=getattr(self._engine, "_last_screenshot_is_window", True),
            window_rect=window_rect,
            image_width=img_w, image_height=img_h,
            has_alpha=has_alpha,
            dpr_hint=dpr_hint,
        )

    @staticmethod
    def _crop_fullscreen_to_window(screenshot_bgr, converter: "ScreenCoordConverter") -> tuple:
        """全屏降级场景下把 L7 视觉指纹搜索范围裁剪到窗口区域内。"""
        if converter.is_window_screenshot or converter.window_rect is None:
            return screenshot_bgr, 0, 0
        try:
            scale = converter.scale_factor()
            if scale is None:
                return screenshot_bgr, 0, 0
            scale_x, scale_y = scale
            wx, wy, ww, wh = converter.window_rect
            img_h, img_w = screenshot_bgr.shape[0], screenshot_bgr.shape[1]
            x0 = max(0, int(wx * scale_x))
            y0 = max(0, int(wy * scale_y))
            x1 = min(img_w, int((wx + ww) * scale_x))
            y1 = min(img_h, int((wy + wh) * scale_y))
            if x1 <= x0 or y1 <= y0:
                return screenshot_bgr, 0, 0
            return screenshot_bgr[y0:y1, x0:x1], x0, y0
        except Exception as exc:
            logger.debug("[L7] 全屏降级窗口区域裁剪失败（忽略，退回全屏搜索）: %s", exc)
            return screenshot_bgr, 0, 0

    @staticmethod
    def _screenshot_image_info(screenshot_bytes: bytes) -> Optional[tuple]:
        """探测截图字节的像素尺寸 + 是否含 alpha 通道（PNG RGBA）。"""
        try:
            from io import BytesIO

            from PIL import Image
            img = Image.open(BytesIO(screenshot_bytes))
            has_alpha = img.mode in ("RGBA", "LA", "PA")
            return (img.width, img.height, has_alpha)
        except Exception as exc:
            logger.debug("[ScreenCoord] 截图尺寸探测失败（忽略）: %s", exc)
            return None

    @staticmethod
    def _decode_screenshot_to_bgr(screenshot_bytes: bytes):
        """把 take_screenshot() 返回的原始字节（通常是 PNG）解码为 OpenCV BGR ndarray。"""
        try:
            import cv2
            import numpy as np
        except ImportError:
            logger.debug("[L7] cv2/numpy 未安装，跳过视觉指纹解码")
            return None
        arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    # ── L6 / L8：LLM 选择器推断与视觉定位能力 ──────

    _LLM_SELECTOR_PROMPT = (
        "你是一个 UI 自动化专家。根据以下可交互元素摘要和元素描述，给出该元素的精确定位方式。\n\n"
        "元素描述：「{desc}」\n\n"
        "当前界面可交互元素摘要：\n{summary}\n\n"
        "重要规则：\n"
        "1. 必须精确匹配元素描述「{desc}」对应的真实元素；不得退而求其次挑语义相近的其他元素。\n"
        "2. 如果摘要中找不到与「{desc}」精确对应的元素，所有字段必须返回 null，confidence 必须 0。\n"
        "3. 宁可返回 null 也不可猜测近似元素。\n\n"
        "请返回 JSON（只返回 JSON，不要任何解释）：\n"
        '{{"selector": "选择器或 null", "role": "role 或 null", '
        '"role_name": "accessible name 或 null", "text": "精确可见文字或 null", '
        '"confidence": 0.0}}'
    )

    _LLM_SELECTOR_MIN_CONFIDENCE = 0.7

    @staticmethod
    def _llm_text_matches_element(text: str, element_name: str) -> bool:
        """L6 text 命中分支的一致性检查。"""
        if not text or not element_name:
            return False
        core = element_name
        if "-" in core:
            core = core.rsplit("-", 1)[-1]
        t = text.strip()
        c = core.strip()
        if not t or not c:
            return False
        return t in c or c in t

    async def _l6_llm_selector(self, element_name: str) -> ResolveResult:
        """L6：AX/UIA/DOM 摘要 → LLM 纯文本推断 selector/role/text → 三级验证。"""
        if self._vision_client is None:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        try:
            summary = await self._engine.get_accessibility_summary()
        except Exception as exc:
            logger.debug("[L6] get_accessibility_summary 异常（忽略）: %s", exc)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        if not summary:
            logger.debug("[L6] 引擎未提供可访问性摘要（未 override 或为空），跳过 LLM_SELECTOR")
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        prompt = self._LLM_SELECTOR_PROMPT.format(desc=element_name, summary=summary)
        try:
            raw = await self._vision_client.chat(prompt)
        except Exception as exc:
            logger.debug("[L6] vision_client.chat 异常（忽略）: %s", exc)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        data = self._extract_json_dict(raw)
        if data is None:
            logger.debug("[L6] LLM 返回无法解析为 JSON: %r", raw)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < self._LLM_SELECTOR_MIN_CONFIDENCE:
            logger.debug("[L6] confidence 过低 (%.2f < %.2f)，跳过", confidence, self._LLM_SELECTOR_MIN_CONFIDENCE)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        sel = (data.get("selector") or "").strip() or None
        role = (data.get("role") or "").strip() or None
        role_name = (data.get("role_name") or "").strip() or None
        text = (data.get("text") or "").strip() or None

        try:
            if sel:
                element = await self._engine.find_by_id(sel)
                if element is not None:
                    logger.debug("[L6] resolved '%s' via selector=%r", element_name, sel)
                    return ResolveResult(
                        element=element, level=ResolveLevel.L6_LLM_SELECTOR,
                        element_name=element_name,
                        heal_text=text or "", heal_role=role or "",
                        heal_role_name=role_name or "",
                        fallback_strategy=ResolveStrategy.MAP_EXACT.name,
                        heal_llm_prompt=self._bounded_llm_prompt(prompt),
                    )
            if role:
                element = await self._engine.find_by_role(role, name=role_name or "")
                if element is not None:
                    logger.debug("[L6] resolved '%s' via role=%r name=%r", element_name, role, role_name)
                    return ResolveResult(
                        element=element, level=ResolveLevel.L6_LLM_SELECTOR,
                        element_name=element_name,
                        heal_text=text or "", heal_role=role or "",
                        heal_role_name=role_name or "",
                        fallback_strategy=ResolveStrategy.ROLE_MATCH.name,
                        heal_llm_prompt=self._bounded_llm_prompt(prompt),
                    )
            if text:
                if not self._llm_text_matches_element(text, element_name):
                    logger.debug(
                        "[L6] text=%r 与 element_name=%r 不一致（疑似 LLM 近似匹配），跳过 text 分支",
                        text, element_name,
                    )
                else:
                    element = await self._engine.find_by_text(text)
                    if element is not None:
                        logger.debug("[L6] resolved '%s' via text=%r", element_name, text)
                        return ResolveResult(
                            element=element, level=ResolveLevel.L6_LLM_SELECTOR,
                            element_name=element_name,
                            heal_text=text or "", heal_role=role or "",
                            heal_role_name=role_name or "",
                            fallback_strategy=ResolveStrategy.TEXT_MATCH.name,
                            heal_llm_prompt=self._bounded_llm_prompt(prompt),
                        )
        except Exception as exc:
            logger.debug("[L6] 验证阶段异常（忽略）: %s", exc)

        logger.debug(
            "[L6] 验证全部失败: desc=%r sel=%r role=%r text=%r", element_name, sel, role, text,
        )
        return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

    async def resolve_via_l4_prompt(self, prompt: str) -> ResolveResult:
        """click[prompt="..."] 专用：跳过完整降级链，直接走 L8 视觉定位。"""
        if self._vision_client is None:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=prompt)

        try:
            screenshot = await self._engine.take_screenshot()
            if not screenshot:
                return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=prompt)
            px, py = await self._vision_client.locate(prompt, screenshot)
        except Exception as exc:
            logger.debug("[L8-prompt] vision_client.locate 异常（忽略）: %s", exc)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=prompt)

        converter = await self._build_screen_converter(screenshot)
        screen_coords = converter.to_screen(px, py)
        if screen_coords is None:
            logger.debug("[L8-prompt] 坐标换算失败（截图解码/阴影降级场景等），降级 NOT_FOUND: %r", prompt)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=prompt)
        cx, cy = screen_coords

        logger.debug("[L8-prompt] resolved via 自定义 prompt at (%.1f, %.1f): %r", cx, cy, prompt)

        element = ElementRef(element_id=f"llm_visual_prompt:{prompt}", raw=None, coords=(cx, cy))
        return ResolveResult(element=element, level=ResolveLevel.L8_LLM_VISUAL, element_name=prompt)

    async def _l8_llm_visual(self, element_name: str) -> ResolveResult:
        """L8：截图 → vision_client.locate() 坐标定位 → 坐标句柄（供 pyautogui 分支）。"""
        if self._vision_client is None:
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        try:
            screenshot = await self._engine.take_screenshot()
            if not screenshot:
                return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
            usage = self.get_element_usage(element_name)
            prompt = _build_l4_locate_prompt(element_name, usage, self._step_context)
            px, py = await self._vision_client.locate(prompt, screenshot)
        except Exception as exc:
            logger.debug("[L8] vision_client.locate 异常（忽略）: %s", exc)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        converter = await self._build_screen_converter(screenshot)
        screen_coords = converter.to_screen(px, py)
        if screen_coords is None:
            logger.debug(
                "[L8] 坐标换算失败（截图解码/阴影降级场景等），降级 NOT_FOUND: %r", element_name,
            )
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)
        cx, cy = screen_coords

        logger.debug("[L8] resolved '%s' via LLM 视觉定位 at (%.1f, %.1f)", element_name, cx, cy)

        if not await self._l8_judge_verify(element_name, screenshot):
            logger.debug("[L8] judge 二次验证未通过，降级 NOT_FOUND: %r", element_name)
            return ResolveResult(element=None, level=ResolveLevel.NOT_FOUND, element_name=element_name)

        element = ElementRef(element_id=f"llm_visual:{element_name}", raw=None, coords=(cx, cy))
        return ResolveResult(
            element=element,
            level=ResolveLevel.L8_LLM_VISUAL,
            element_name=element_name,
            heal_llm_prompt=self._bounded_llm_prompt(prompt),
        )

    async def _l8_judge_verify(self, element_name: str, screenshot: bytes) -> bool:
        """L8 locate 成功后的 judge 二次验证。"""
        if self._vision_client is None:
            return True
        question = (
            f"截图中是否存在可交互的（可点击的）UI元素，其文字或语义与「{element_name}」精确对应？"
            f"必须精确匹配，不允许近似匹配。"
            f"注意：必须是可点击的UI元素本身（按钮/链接/菜单项等），"
            f"不是页面文本内容或活动记录中提到的文字。"
        )
        try:
            verdict = await self._vision_client.judge(question, screenshot)
        except Exception as exc:
            logger.debug("[L8] judge 二次验证异常（保守降级）: %s", exc)
            return False
        logger.debug("[L8] judge 二次验证: element=%r verdict=%s", element_name, verdict)
        return verdict

    @staticmethod
    def _extract_json_dict(raw: str) -> Optional[dict]:
        """从 LLM 文本响应中提取首个 JSON 对象并解析为 dict；失败返回 None。"""
        import json
        import re as _re

        if not raw:
            return None
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None


def _self_heal_promote_if_ready(
    element_id: str,
    strategy_name: str,
    router_proxy_view,
    element_name: str = "",
    promote_threshold: int = 3,
) -> None:
    """自愈策略命中达到阈值时，将 source=fallback 升级为 source=default。

    hit_count >= promote_threshold 时调用 router_proxy_view.promote()，将该
    策略从 fallback（自愈候选）升级为 default（正式默认路由），下次
    route_v2() 直接走定制链路（idx=0）。

    任何异常均静默（不影响主流程）。
    """
    try:
        rec = router_proxy_view.get_record(element_id)
        if rec is not None and rec.hit_count >= promote_threshold and not rec.is_default_source:
            router_proxy_view.promote(element_id)
            logger.info(
                "[self-heal] 策略升级 default: element=%r id=%s strategy=%s "
                "hit_count=%d（阈值 %d）",
                element_name, element_id, rec.router_proxy,
                rec.hit_count, promote_threshold,
            )
        elif rec is not None and rec.is_default_source:
            logger.debug(
                "[self-heal] 策略已是 default: element=%r strategy=%s，无需升级",
                element_name, rec.router_proxy,
            )
    except Exception as exc:
        logger.debug("[self-heal] promote 失败（静默）: %s", exc)


# ── 降级兜底自愈 ───────────────────────────────────────────────────────

def _self_heal_l4_capture(
    name: str,
    screenshot_bytes: bytes,
    coords: tuple,
    dpr: float,
    fingerprint_resolver: Optional[object] = None,
    rosetta_map_view: Optional[object] = None,
    element_map: Optional[object] = None,
) -> None:
    """L4 LLM 视觉定位成功后，自动捕获新指纹并写入 DB。

    双层写入：
      1. fingerprint_resolver.capture()   → 本机 PNG + fingerprint_index.json
      2. rosetta_map_view.update_pattern() → DB {app}_rosetta_map 表的 pattern 字段

    任何异常均静默（自愈失败不影响主流程）。
    """
    if fingerprint_resolver is None and rosetta_map_view is None:
        return

    # 1) 本机 PNG 捕获
    if fingerprint_resolver is not None:
        try:
            screenshot_bgr = Resolver._decode_screenshot_to_bgr(screenshot_bytes)
            if screenshot_bgr is not None:
                padding = 24
                h, w = screenshot_bgr.shape[:2]
                cx, cy = int(coords[0]), int(coords[1])
                x1 = max(0, cx - padding); y1 = max(0, cy - padding)
                x2 = min(w, cx + padding); y2 = min(h, cy + padding)
                fingerprint_resolver.capture(
                    name, screenshot_bgr,
                    bbox=(x1, y1, x2-x1, y2-y1),
                    dpi_scale=float(dpr) if dpr else 2.0,
                )
                logger.info("[self-heal] L4→fingerprint 录新指纹: %r", name)
        except Exception as exc:
            logger.debug("[self-heal] fingerprint capture 失败（非致命）: %s", exc)

    # 2) DB pattern 更新 — 先探活，不可用时回退本地 PNG
    if rosetta_map_view is not None and element_map is not None:
        try:
            if True:
                logger.debug("[self-heal] DB 探活失败，跳过 pattern 回写: %r", name)
                return
            entry = element_map.find(name)
            if entry and entry.id:
                # 裁切元素截图 → z85 编码（R2 修复：改用 core/ 内部自持实现，
                # 不再 import uita.tools.agent_rosetta，见 core/resolver/_pattern_helpers.py）
                from uita.core.resolver._pattern_helpers import encode_pattern as _encode_pattern

                # 直接用 Pillow 裁切（避免跨模块 mock 依赖）
                try:
                    from PIL import Image
                    import io as _io
                    img = Image.open(_io.BytesIO(screenshot_bytes))
                    px1 = max(0, int((coords[0] - 24) * dpr))
                    py1 = max(0, int((coords[1] - 24) * dpr))
                    px2 = min(img.width, int((coords[0] + 24) * dpr))
                    py2 = min(img.height, int((coords[1] + 24) * dpr))
                    crop = img.crop((px1, py1, px2, py2))
                    buf = _io.BytesIO()
                    crop.save(buf, format="PNG")
                    pattern_val = _encode_pattern(buf.getvalue())
                except Exception:
                    pattern_val = None

                if pattern_val:
                    rosetta_map_view.update_pattern(
                        entry.id, pattern_val,
                        dpi=float(dpr) if dpr else 2.0,
                    )
                    logger.info("[self-heal] L4→DB pattern 已更新: %r → %s",
                                name, entry.id)
        except Exception as exc:
            logger.debug("[self-heal] DB pattern update 失败（非致命）: %s", exc)
