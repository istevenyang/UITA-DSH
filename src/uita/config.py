"""UITA-DSH — 全局路径与可调参数。

配置原则：
  - 所有 LLM / 视觉模型调用统一使用环境变量 `DSH_API_URL` 与 `DSH_API_KEY`
  - 敏感凭据（API Key 等）只从环境变量或本地 `dsh_settings.json` 读取，禁止硬编码
  - 业务 App 专属配置归位各 `apps/<app_name>/configs.py`
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

# ── 自定义日志级别 NOTICE (25) ────────────────────────────────────────────────
NOTICE = 25
logging.addLevelName(NOTICE, "NOTICE")


def _parse_str_list_env(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or list(default)


# ── 项目根目录与包目录 ─────────────────────────────────────────────────────────
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.dirname(os.path.dirname(PKG_DIR))


def _resolve_root_dir() -> str:
    env_root = os.environ.get("UITA_ROOT", "").strip() or os.environ.get("UITA_WORKSPACE", "").strip()
    if env_root:
        return os.path.abspath(env_root)
    if os.path.isdir(os.path.join(_SRC_ROOT, "apps")) or os.path.isdir(
        os.path.join(_SRC_ROOT, "cases")
    ):
        return _SRC_ROOT
    return os.getcwd()


ROOT_DIR = _resolve_root_dir()
WORKSPACE_DIR = ROOT_DIR


def _resolve_user_data_dir() -> str:
    env_dir = os.environ.get("UITA_DATA_DIR", "").strip()
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return os.path.abspath(env_dir)
    if os.path.isdir(os.path.join(_SRC_ROOT, "apps")) or os.path.isdir(
        os.path.join(_SRC_ROOT, "cases")
    ):
        return ROOT_DIR
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "UITA")
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/UITA")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        d = os.path.join(base, "uita")
    os.makedirs(d, exist_ok=True)
    return d


USER_DATA_DIR = _resolve_user_data_dir()

APPS_DIR = os.path.join(ROOT_DIR, "apps")
CASES_DIR = os.path.join(ROOT_DIR, "cases")
REPORT_DIR = os.path.join(USER_DATA_DIR, "report")
# 日志默认统一输出至项目根目录 logs/ 下（可通过 UITA_LOG_DIR 覆盖）
LOG_DIR = os.path.abspath(os.environ.get("UITA_LOG_DIR", "").strip() or os.path.join(ROOT_DIR, "logs"))
SCREENSHOT_DIR = os.path.join(REPORT_DIR, "screenshots")
TRACE_DIR = os.path.join(REPORT_DIR, "traces")

LOG_LEVEL = os.environ.get("UITA_LOG_LEVEL", "INFO").upper()


def setup_logging(level: str = "", log_dir: str = "", force: bool = False) -> dict[str, str]:
    """初始化控制台与根目录 `logs/` 分类文件日志（execution / actions / kernel / uita）。"""
    from uita.core.logging_manager import configure_categorized_logging

    effective = (level or LOG_LEVEL or "INFO").upper()
    target_dir = os.path.abspath(log_dir) if log_dir else LOG_DIR
    return configure_categorized_logging(target_dir, console_level=effective, force=force)


def get_app_path(app_name: str, platform: str = "") -> str:
    """获取被测 App 路径（优先环境变量 UITA_<APP>_PATH，其次 AppRegistry）。"""
    env_key = f"UITA_{app_name.upper()}_PATH"
    if os.environ.get(env_key, "").strip():
        return os.environ[env_key].strip()
    try:
        from uita.core.platform.app_registry import AppRegistry
        from uita.core.registry_common import UNIVERSAL_PLATFORM, resolve_universal_platform

        plat = platform or resolve_universal_platform(UNIVERSAL_PLATFORM)
        cfg = AppRegistry.get(app_name, plat)
        return str(cfg.app_path or "")
    except Exception:
        return ""


# ── CDP（Chrome DevTools Protocol / Electron）默认参数 ─────────────────────────
DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = int(os.environ.get("UITA_CDP_PORT", "9222"))
BASE_CDP_PORT = DEFAULT_CDP_PORT
CDP_CONNECT_TIMEOUT_MS = 15_000
CDP_WAIT_TIMEOUT = float(os.environ.get("UITA_CDP_WAIT_TIMEOUT", "15.0"))
ACTION_DEFAULT_TIMEOUT_MS = 10_000
WAIT_DEFAULT_TIMEOUT_MS = 15_000

# ── 执行节奏与页面检测 ────────────────────────────────────────────────────────
STEP_DELAY_MS = int(os.environ.get("UITA_STEP_DELAY_MS", "500"))
POST_LAUNCH_DELAY_MS = int(os.environ.get("UITA_POST_LAUNCH_DELAY_MS", "2000"))
ENABLE_PAGE_HASH_VERIFY: bool = os.environ.get("UITA_ENABLE_PAGE_HASH_VERIFY", "1") == "1"
UITA_PAGE_DETECT_WAIT_MS: int = int(os.environ.get("UITA_PAGE_DETECT_WAIT_MS", "500"))

# ── 截图策略 ──────────────────────────────────────────────────────────────────
SCREENSHOT_ON_EVERY_STEP = os.environ.get("UITA_SCREENSHOT_EVERY_STEP", "1") == "1"
SCREENSHOT_ON_EACH_STEP = SCREENSHOT_ON_EVERY_STEP
SCREENSHOT_ON_FAIL_ONLY = not SCREENSHOT_ON_EVERY_STEP

# ── 生命周期与重试策略 ────────────────────────────────────────────────────────
RESTART_BEFORE_FIRST_CASE: bool = (
    os.environ.get("UITA_RESTART_BEFORE_FIRST_CASE", "1") == "1"
)
RESTART_BETWEEN_CASES: bool = (
    os.environ.get("UITA_RESTART_BETWEEN_CASES", "1") == "1"
)
SOFT_RESET_TIMEOUT: float = float(os.environ.get("UITA_SOFT_RESET_TIMEOUT", "10.0"))
LAUNCH_POLICY_TIMEOUT: float = float(
    os.environ.get("UITA_LAUNCH_POLICY_TIMEOUT", "15.0")
)
CASE_MAX_EXECUTIONS: int = max(
    1,
    int(os.environ.get("UITA_CASE_MAX_EXECUTIONS", "3")),
)
ENABLE_FAILED_REQUEUE: bool = os.environ.get("UITA_ENABLE_FAILED_REQUEUE", "1") == "1"
MAX_RETRY_PER_CASE: int = max(0, CASE_MAX_EXECUTIONS - 1)
DISPATCH_BATCH_ID: str = os.environ.get("UITA_BATCH_ID", "").strip()

# ── 定位器与语义匹配阈值 ──────────────────────────────────────────────────────
SEMANTIC_MATCH_THRESHOLD: float = float(
    os.environ.get("UITA_SEMANTIC_MATCH_THRESHOLD", "0.75")
)

# ── L7 视觉指纹目录 & 匹配阈值 ──────────────────────────────────────────────
VISUAL_FINGERPRINT_DIR = os.path.join(USER_DATA_DIR, "data", "visual_fingerprints")
VISUAL_FINGERPRINT_MIN_CONFIDENCE = float(
    os.environ.get("UITA_FP_MIN_CONFIDENCE", "0.80")
)
ASSERT_FINGERPRINT_MIN_CONFIDENCE = float(
    os.environ.get("UITA_ASSERT_FP_MIN_CONFIDENCE", "0.95")
)

# ── L8 OCR 文字识别兜底（Tesseract CLI，跨平台零 pip 依赖） ─────────────────
OCR_LANG: str = os.environ.get("UITA_OCR_LANG", "chi_sim+eng")
OCR_TIMEOUT_SEC: float = float(os.environ.get("UITA_OCR_TIMEOUT_SEC", "8.0"))
OCR_MIN_TEXT_LEN: int = int(os.environ.get("UITA_OCR_MIN_TEXT_LEN", "2"))

# ── RouterProxy 候选策略持久化目录 ────────────────────────────────────────────
ROUTER_PROXY_CANDIDATE_DIR = os.path.join(
    USER_DATA_DIR, "data", "router_proxy_candidates"
)

# ── Rosetta & RouterProxy 存储配置（默认本地 JSON；支持通过抽象接口接入远端数据库） ─
USE_LOCAL_ROSETTA: bool = os.environ.get("UITA_USE_LOCAL_ROSETTA", "1") == "1"
UITA_REMOTE_DB_URL: str = os.environ.get("UITA_REMOTE_DB_URL", "").strip()
UITA_ROSETTA_STORE_CLASS: str = os.environ.get("UITA_ROSETTA_STORE_CLASS", "").strip()
UITA_ROUTER_PROXY_STORE_CLASS: str = os.environ.get("UITA_ROUTER_PROXY_STORE_CLASS", "").strip()

# ── 弹窗自动消解最大轮次 ──────────────────────────────────────────────────────
MAX_DISMISS_ROUNDS = 3

# ── L8 视觉模型开关与配置（统一复用 DSH_API_URL / DSH_API_KEY） ───────────────
ENABLE_L8_VISION = os.environ.get("UITA_ENABLE_L8_VISION", "1") == "1"
ENABLE_L4_VISION = ENABLE_L8_VISION
UITA_ENABLE_L8_ASSERT: bool = os.environ.get("UITA_ENABLE_L8_ASSERT", "1") == "1"
UITA_L8_PROVIDER: str = os.environ.get("UITA_L8_PROVIDER", "dsh").strip().lower()
UITA_L8_QUESTION_TEMPLATE: str = os.environ.get(
    "UITA_L8_QUESTION_TEMPLATE",
    "请判断当前界面中是否存在以下元素：{description}",
)

# ── API 自动化执行器配置（--mode api-runner） ─────────────────────────────────
API_DEFAULT_TIMEOUT_S: float = float(
    os.environ.get("UITA_API_DEFAULT_TIMEOUT_S", "15.0")
)
API_DEFAULT_VERIFY_SSL: bool = (
    os.environ.get("UITA_API_DEFAULT_VERIFY_SSL", "0") == "1"
)
API_CONNECT_RETRIES: int = int(os.environ.get("UITA_API_CONNECT_RETRIES", "2"))
API_RETRY_BACKOFF_S: float = float(
    os.environ.get("UITA_API_RETRY_BACKOFF_S", "0.5")
)

# ── 性能采集与基线判定（--mode perf） ─────────────────────────────────────────
PERF_REPEAT_DEFAULT: int = max(1, int(os.environ.get("UITA_PERF_REPEAT", "5")))
PERF_SAMPLE_INTERVAL_MS: int = max(
    50, int(os.environ.get("UITA_PERF_SAMPLE_INTERVAL_MS", "200"))
)
PERF_ENABLE_RESOURCE_SAMPLING: bool = (
    os.environ.get("UITA_PERF_ENABLE_RESOURCE_SAMPLING", "1") == "1"
)
PERF_WARMUP_ENABLED: bool = os.environ.get("UITA_PERF_WARMUP", "1") == "1"
PERF_BASELINE_PATH: str = os.environ.get("UITA_PERF_BASELINE_PATH", "").strip()
PERF_AI_ANALYSIS_ENABLED: bool = (
    os.environ.get("UITA_PERF_AI_ANALYSIS", "0") == "1"
)
PERF_AI_MODEL: str = os.environ.get("UITA_PERF_AI_MODEL", "default-model").strip()
PERF_AI_TIMEOUT_S: int = max(
    60, int(os.environ.get("UITA_PERF_AI_TIMEOUT_S", "900"))
)
PERF_AI_STALL_S: int = max(
    0, int(os.environ.get("UITA_PERF_AI_STALL_S", "300"))
)
PERF_AI_LOOP_REPEAT: int = max(
    0, int(os.environ.get("UITA_PERF_AI_LOOP_REPEAT", "5"))
)
PERF_AI_ARTIFACT_GRACE_S: int = max(
    0, int(os.environ.get("UITA_PERF_AI_ARTIFACT_GRACE_S", "450"))
)
PERF_AI_RETRY_ATTEMPTS: int = max(
    0, int(os.environ.get("UITA_PERF_AI_RETRY_ATTEMPTS", "1"))
)
PERF_AI_HEARTBEAT_S: int = max(
    10, int(os.environ.get("UITA_PERF_AI_HEARTBEAT_S", "120"))
)
PERF_AI_FALLBACK_ENABLED: bool = (
    os.environ.get("UITA_PERF_AI_FALLBACK", "1") == "1"
)
PERF_AI_REQUIRED_SECTIONS: tuple[str, ...] = (
    "reading",
    "sources",
    "overall",
    "scenarios",
    "issues",
    "actions",
    "questions",
    "appa",
    "appb",
)
PERF_AI_COMPARE_RUN: str = (
    os.environ.get("UITA_PERF_AI_COMPARE_RUN", "auto").strip() or "auto"
)

# ── DSH（Deterministic Skill Harness）与全局 LLM 统一配置 ────────────────────
# 所有模型调用（DSH 规划器、L6/L8 视觉定位与判定、性能分析）统一读取 DSH_API_URL 和 DSH_API_KEY
UITA_DSH_HOST: str = os.environ.get("UITA_DSH_HOST", "").strip() or "127.0.0.1"
UITA_DSH_PORT: int = int(os.environ.get("UITA_DSH_PORT", "8765") or 8765)
UITA_DSH_TOKEN: str = os.environ.get("UITA_DSH_TOKEN", "").strip()
UITA_DSH_CDP_PORT: int = int(os.environ.get("UITA_DSH_CDP_PORT", str(DEFAULT_CDP_PORT)))
UITA_DSH_MODE: str = (
    os.environ.get("UITA_DSH_MODE", "supervised").strip().lower() or "supervised"
)
UITA_DSH_MAX_STEPS: int = int(os.environ.get("UITA_DSH_MAX_STEPS", "30") or 30)
UITA_DSH_TIMEOUT_S: int = int(os.environ.get("UITA_DSH_TIMEOUT_S", "600") or 600)
UITA_DSH_MAX_HEAL_ROUNDS: int = int(os.environ.get("UITA_DSH_MAX_HEAL_ROUNDS", "1") or 1)
UITA_DSH_APPROVAL_TIMEOUT_S: int = int(
    os.environ.get("UITA_DSH_APPROVAL_TIMEOUT_S", "300") or 300
)
UITA_DSH_OUT_ROOT: str = os.path.join(REPORT_DIR, "dsh_runs")
UITA_DSH_SESSION_DIR: str = os.path.join(REPORT_DIR, "dsh_sessions")
UITA_DSH_SSE_KEEPALIVE_S: float = max(
    1.0, float(os.environ.get("UITA_DSH_SSE_KEEPALIVE_S", "15"))
)
UITA_DSH_LOOP_REPEAT: int = int(
    os.environ.get("UITA_DSH_LOOP_REPEAT", "4") or 4
)
UITA_DSH_STALL_S: int = int(
    os.environ.get("UITA_DSH_STALL_S", "300") or 300
)
UITA_DSH_HEARTBEAT_S: int = int(
    os.environ.get("UITA_DSH_HEARTBEAT_S", "60") or 60
)
UITA_DSH_OBSERVE_AFTER_ACTION: bool = (
    os.environ.get("UITA_DSH_OBSERVE_AFTER_ACTION", "1").strip().lower()
    not in {"0", "false", "no"}
)

DSH_API_URL: str = os.environ.get("DSH_API_URL", "").strip()
DSH_API_KEY: str = os.environ.get("DSH_API_KEY", "").strip()

UITA_DSH_LLM_URL_ENVS: tuple = ("DSH_API_URL", "UITA_DSH_LLM_URL")
UITA_DSH_LLM_URL: str = (
    DSH_API_URL or os.environ.get("UITA_DSH_LLM_URL", "").strip()
)
UITA_DSH_API_KEY_ENVS: tuple = ("DSH_API_KEY", "UITA_DSH_API_KEY")
UITA_DSH_MODEL: str = (
    os.environ.get("UITA_DSH_MODEL", "default-model").strip() or "default-model"
)
UITA_DSH_MODEL_FALLBACKS: tuple = tuple(
    _parse_str_list_env(
        "UITA_DSH_MODEL_FALLBACKS",
        ("default-model", "fallback-model"),
    )
)
UITA_DSH_PLANNER_RETRIES: int = max(
    1, int(os.environ.get("UITA_DSH_PLANNER_RETRIES", "2"))
)
UITA_DSH_LLM_TIMEOUT_S: int = int(
    os.environ.get("UITA_DSH_LLM_TIMEOUT_S", "120") or 120
)
UITA_DSH_HTTP_TIMEOUT_S: float = float(UITA_DSH_LLM_TIMEOUT_S)
UITA_DSH_LLM_NATIVE_TOOLS: bool = (
    os.environ.get("UITA_DSH_LLM_NATIVE_TOOLS", "0").strip().lower()
    not in {"0", "false", "no"}
)
UITA_DSH_AUTO_COMPILE: bool = (
    os.environ.get("UITA_DSH_AUTO_COMPILE", "1").strip() != "0"
)
UITA_DSH_USE_VISION: bool = (
    os.environ.get("UITA_DSH_USE_VISION", "1").strip() != "0"
)

# ── 用例自定义变量表（供 TCS 文本中 ${{VAR}} 占位符替换） ────────────────────
CASE_VARIABLES: dict[str, str] = {}


def refresh_case_variables() -> dict[str, str]:
    """从环境变量 UITA_VAR_<NAME> 刷新用例变量表。"""
    prefix = "UITA_VAR_"
    for k, v in os.environ.items():
        if k.startswith(prefix) and len(k) > len(prefix):
            CASE_VARIABLES[k[len(prefix):]] = v
    return CASE_VARIABLES


refresh_case_variables()
