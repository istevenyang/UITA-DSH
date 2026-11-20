"""测试统一整数层级（L1~L9）与双层自定义降级链（App 适配级 + 元素自定义级）。"""
from __future__ import annotations

import json
from pathlib import Path

from uita.core.platform.element_map_loader import ElementMapLoader
from uita.core.platform.router_proxy_loader import RouterProxyLoader
from uita.core.resolver.resolver import ResolveLevel
from uita.core.resolver.router import ActionRouter, ResolveStrategy, parse_proxy_tokens


def test_resolve_level_uses_unified_integer_names() -> None:
    """ResolveLevel 统一使用 L1 ~ L9 整数编号，不含小数/字母后缀。"""
    expected = [
        "L1_MAP_EXACT",
        "L2_SEMANTIC",
        "L3_TEXT_MATCH",
        "L4_LABEL_MATCH",
        "L5_ROLE_MATCH",
        "L6_LLM_SELECTOR",
        "L7_VISUAL_FP",
        "L8_LLM_VISUAL",
        "L9_QT_PROBE",
        "NOT_FOUND",
    ]
    assert [lvl.name for lvl in ResolveLevel] == expected


def test_parse_proxy_tokens_supports_level_tokens_and_strategy_names() -> None:
    """支持使用 L1~L9 简写或策略名配置自定义降级链。"""
    chain_by_level = parse_proxy_tokens(["L1", "L3", "L7", "L8", "L9"])
    assert chain_by_level == [
        ResolveStrategy.MAP_EXACT,
        ResolveStrategy.TEXT_MATCH,
        ResolveStrategy.VISUAL_FP,
        ResolveStrategy.LLM_VISUAL,
        ResolveStrategy.QT_PROBE,
    ]

    chain_by_name = parse_proxy_tokens(["MAP_EXACT", "SEMANTIC", "ROLE_MATCH"])
    assert chain_by_name == [
        ResolveStrategy.MAP_EXACT,
        ResolveStrategy.SEMANTIC,
        ResolveStrategy.ROLE_MATCH,
    ]


def test_app_level_custom_chain_via_router_proxy_and_app_default(tmp_path: Path) -> None:
    """验证 App 适配级自定义降级链（router_proxy.__default__ 与 app_default_chain）。"""
    router = ActionRouter()

    # 1) 通过 app_default_chain（来自 configs.py extra 或 rosetta_map._meta）
    decision = router.route_v2(
        description="普通元素",
        flat_entry=None,
        app_default_chain=["L3", "L7"],
    )
    assert decision.strategies[:2] == [
        ResolveStrategy.TEXT_MATCH,
        ResolveStrategy.VISUAL_FP,
    ]
    assert decision.skip_self_heal is False

    # 2) 通过 router_proxy.json 的 __default__ 配置 App 级默认链（优先级高于 app_default_chain）
    proxy_file = tmp_path / "router_proxy.json"
    proxy_file.write_text(
        json.dumps({"__default__": {"default": "L5,L8"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    proxy_loader = RouterProxyLoader(str(proxy_file))
    decision2 = router.route_v2(
        description="普通元素",
        flat_entry=None,
        router_proxy_entry=proxy_loader.find("non_existent_id"),
        default_proxy_entry=proxy_loader.default_route(),
        app_default_chain=["L3", "L7"],
    )
    assert decision2.strategies[:2] == [
        ResolveStrategy.ROLE_MATCH,
        ResolveStrategy.LLM_VISUAL,
    ]
    assert decision2.skip_self_heal is False


def test_element_level_custom_chain_via_rosetta_and_router_proxy(tmp_path: Path) -> None:
    """验证元素自定义级降级链（rosetta_map 元素级 strategy_chain 与 router_proxy 按 id 覆盖）。"""
    rosetta_file = tmp_path / "rosetta_map.json"
    rosetta_file.write_text(
        json.dumps(
            {
                "_meta": {
                    "version": "3.0",
                    "app": "sample_web",
                    "default_strategy_chain": "L5,L3,L2",
                },
                "sample_web": {
                    "首页": {
                        "标题": {
                            "id": "el_title",
                            "selector": "h1",
                            "strategy_chain": "L3,L8",
                        },
                        "普通按钮": {
                            "id": "el_btn",
                            "selector": "button.primary",
                        },
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loader = ElementMapLoader(str(rosetta_file), platform="macOS")
    router = ActionRouter()

    # 普通按钮未定义元素级链 -> 走 rosetta_map._meta.default_strategy_chain (L5,L3,L2 打头)
    btn_entry = loader.find("普通按钮")
    assert btn_entry is not None
    btn_decision = router.route_v2(
        description="普通按钮",
        flat_entry=btn_entry,
        app_default_chain=loader.default_strategy_chain(),
    )
    assert btn_decision.strategies[:3] == [
        ResolveStrategy.ROLE_MATCH,
        ResolveStrategy.TEXT_MATCH,
        ResolveStrategy.SEMANTIC,
    ]
    assert btn_decision.skip_self_heal is False

    # 标题定义了元素级 strategy_chain="L3,L8" -> 覆盖 App 默认链，以 L3,L8 打头且 skip_self_heal=True
    title_entry = loader.find("标题")
    assert title_entry is not None
    title_decision = router.route_v2(
        description="标题",
        flat_entry=title_entry,
        app_default_chain=loader.default_strategy_chain(),
    )
    assert title_decision.strategies[:2] == [
        ResolveStrategy.TEXT_MATCH,
        ResolveStrategy.LLM_VISUAL,
    ]
    assert title_decision.skip_self_heal is True

    # 当 router_proxy.json 对 el_title 配置了 runtime_define 时，优先级最高（高于 rosetta_map 元素内联 strategy_chain）
    proxy_file = tmp_path / "router_proxy.json"
    proxy_file.write_text(
        json.dumps(
            {
                "el_title": {
                    "default": "L1,L3",
                    "runtime_define": "L7,L8",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    proxy_loader = RouterProxyLoader(str(proxy_file))
    proxy_decision = router.route_v2(
        description="标题",
        flat_entry=title_entry,
        router_proxy_entry=proxy_loader.find("el_title"),
        app_default_chain=loader.default_strategy_chain(),
    )
    assert proxy_decision.strategies[:2] == [
        ResolveStrategy.VISUAL_FP,
        ResolveStrategy.LLM_VISUAL,
    ]
    assert proxy_decision.skip_self_heal is True
