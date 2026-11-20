"""resolver_factory — 按 (app_name, os_name) 组装 Resolver 的公共工厂函数。"""
from __future__ import annotations

import logging

from uita.config import NOTICE
from uita.core.actions.judgers.l8_integration import build_vision_client_safe, configure_l8_fallback
from uita.core.engine.base import EngineBase
from uita.core.platform.app_registry import AppRegistry
from uita.core.platform.element_map_loader import ElementMapLoader
from uita.core.platform.router_proxy_loader import RouterProxyLoader, router_proxy_path_for
from uita.core.resolver.resolver import Resolver
from uita.core.resolver.router_proxy_candidate_store import RouterProxyCandidateStore

logger = logging.getLogger(__name__)


def build_resolver_for_app(engine: EngineBase, app_name: str, os_name: str) -> Resolver:
    """为 (app_name, os_name) 组装一个完整的 Resolver。"""
    from uita.config import (
        ROUTER_PROXY_CANDIDATE_DIR,
        UITA_ENABLE_L8_ASSERT,
        UITA_L8_PROVIDER,
        UITA_L8_QUESTION_TEMPLATE,
    )
    from uita.core.platform.rosetta_map_version_selector import select_rosetta_map

    app_config = AppRegistry.get(app_name, os_name)
    rosetta_map_path = select_rosetta_map(
        app_config.rosetta_map_path, app_config.app_version,
    )
    element_map = ElementMapLoader(rosetta_map_path, os_name)
    element_map.load()
    logger.log(NOTICE, "[resolver:init] app=%s map-ready; vision-start", app_name)
    overlay_candidates = app_config.extra.get("overlay_candidates")
    overlay_ignore = app_config.extra.get("overlay_ignore")

    router_proxy_loader = None
    candidate_store = None
    try:
        router_proxy_path = router_proxy_path_for(rosetta_map_path)
        router_proxy_loader = RouterProxyLoader(router_proxy_path)
        candidate_store = RouterProxyCandidateStore(
            app_name, os_name, candidate_dir=ROUTER_PROXY_CANDIDATE_DIR,
        )
    except Exception as exc:
        logger.debug(
            "[resolver:init] app=%s RouterProxy 装配跳过: %s", app_name, exc,
        )

    vision_client = None
    try:
        vision_client = build_vision_client_safe(UITA_L8_PROVIDER)
    except Exception as exc:
        logger.debug("[resolver:init] app=%s vision_client 装配异常: %s", app_name, exc)

    try:
        l8_vision_client = configure_l8_fallback(
            enabled=UITA_ENABLE_L8_ASSERT,
            provider=UITA_L8_PROVIDER,
            question_template=UITA_L8_QUESTION_TEMPLATE,
        )
        if vision_client is None:
            vision_client = l8_vision_client
    except Exception as exc:
        logger.debug(
            "[resolver:init] app=%s L8 视觉兜底装配跳过: %s", app_name, exc,
        )

    logger.log(NOTICE, "[resolver:init] app=%s vision-ready; fingerprint-start", app_name)
    fingerprint_resolver = None
    try:
        from uita.config import VISUAL_FINGERPRINT_DIR as _fp_dir
        from uita.core.resolver.l7_visual_fingerprint import VisualFingerprintResolver
        _normalized_os = os_name.lower()
        _platform_suffix = (
            "mac" if _normalized_os in {"macos", "darwin", "mac"}
            else "win" if _normalized_os in {"windows", "win"}
            else "linux" if _normalized_os == "linux"
            else None
        )
        if _platform_suffix is None:
            raise ValueError(f"不支持视觉指纹的平台: {os_name!r}")
        fingerprint_resolver = VisualFingerprintResolver(
            app_name, _platform_suffix,
            fingerprint_dir=_fp_dir,
        )
        for _entry_name, _entry in element_map.all_entries().items():
            _patterns = getattr(_entry, "patterns", None) or (
                [_entry.pattern] if getattr(_entry, "pattern", None) else []
            )
            _patterns = [
                p for p in _patterns if p and (p.startswith("z85zl:") or p.startswith("z85:"))
            ]
            if _patterns:
                fingerprint_resolver.ensure_from_patterns(_entry_name, _patterns)
    except Exception:
        fingerprint_resolver = None

    logger.log(NOTICE, "[resolver:init] app=%s fingerprint-ready", app_name)
    try:
        from uita.core.resolver.l8_ocr import inject_l8_ocr_fallback
        inject_l8_ocr_fallback()
    except Exception as _ocr_exc:
        logger.debug("[L8][OCR] 装配跳过: %s", _ocr_exc)

    try:
        if fingerprint_resolver is not None:
            from uita.core.resolver.l7_assert_fingerprint import inject_assert_fingerprint_fallback
            inject_assert_fingerprint_fallback(fingerprint_resolver)
    except Exception as _fp_exc:
        logger.debug("[L7][ASSERT_FP] 装配跳过: %s", _fp_exc)

    app_default_chain = (
        app_config.extra.get("default_strategy_chain")
        or element_map.default_strategy_chain()
    )

    return Resolver(
        engine, element_map,
        overlay_candidates=overlay_candidates,
        overlay_ignore=overlay_ignore,
        vision_client=vision_client,
        fingerprint_resolver=fingerprint_resolver,
        router_proxy_loader=router_proxy_loader,
        router_proxy_candidate_store=candidate_store,
        rosetta_map_view=None,
        fallback_view=None,
        router_proxy_view=None,
        default_strategy_chain=app_default_chain,
    )
