"""sample_web 应用配置。"""
from __future__ import annotations

import os

APP_NAME = "sample_web"
DEFAULT_URL = os.environ.get("SAMPLE_WEB_URL", "https://example.com")
