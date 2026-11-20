"""独立 API TCS DSL（不依赖 UI core.dsl）。"""

from .api_parser import ApiParseError, parse_api_file, parse_api_source

__all__ = ["ApiParseError", "parse_api_file", "parse_api_source"]
