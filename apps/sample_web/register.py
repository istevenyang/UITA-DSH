"""sample_web 应用注册：演示如何接入 UITA-DSH。"""
from __future__ import annotations

import os

from apps.sample_web.configs import APP_NAME, DEFAULT_URL
from uita.core.engine.registry import register_engine
from uita.core.platform.app_registry import AppPlatformConfig, AppRegistry, LaunchStrategy
from uita.engine.playwright.engine import PlaywrightEngine

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROSETTA_PATH = os.path.join(_HERE, "rosetta_map.json")
_NAV_PATH = os.path.join(_HERE, "nav_map.json")


@register_engine(APP_NAME, "universal")
class SampleWebEngine(PlaywrightEngine):
    """基于 Playwright 的示例 Web 应用引擎。"""

    def __init__(self, **kwargs):
        super().__init__(
            app_name=APP_NAME,
            start_url=DEFAULT_URL,
            headless=os.environ.get("SAMPLE_WEB_HEADLESS", "1") == "1",
            **kwargs,
        )


for _platform in ("macOS", "Windows", "Linux"):
    AppRegistry.register(
        AppPlatformConfig(
            app_name=APP_NAME,
            platform=_platform,
            launch_strategy=LaunchStrategy.CDP,
            rosetta_map_path=_ROSETTA_PATH,
            nav_map_path=_NAV_PATH,
            extra={"start_url": DEFAULT_URL, "use_local_rosetta": True},
        )
    )
