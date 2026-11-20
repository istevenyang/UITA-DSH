"""DSH 本地配置：界面配置 → 落盘 ``dsh_settings.json`` → 后续启动自动读取。

优先级（高 → 低）：CLI 参数 > 环境变量 > 配置文件 > ``config.py`` 默认值。
配置文件里的值只在对应环境变量缺省时生效，不改变既有 env 语义。

安全（R5）：密钥只写入用户数据目录下的配置文件（权限 ``0600``），对外接口
只返回掩码与「是否已设置」标记，任何日志/响应都不出现明文。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any
from urllib.parse import urlparse

FILENAME = "dsh_settings.json"
VERSION = 1
MODES = ("supervised", "readonly", "auto")
MASK = "••••••"
_APP_RE = re.compile(r"^[A-Za-z0-9_.\-]{0,64}$")

#: 各配置项对应的环境变量（按顺序取第一个非空值）
ENV_KEYS: dict[str, tuple[str, ...]] = {
    "llm_url": ("DSH_API_URL", "UITA_DSH_LLM_URL"),
    "llm_api_key": ("DSH_API_KEY", "UITA_DSH_API_KEY"),
    "llm_model": ("UITA_DSH_MODEL",),
    "port": ("UITA_DSH_PORT",),
    "mode": ("UITA_DSH_MODE",),
    "max_steps": ("UITA_DSH_MAX_STEPS",),
    "timeout_s": ("UITA_DSH_TIMEOUT_S",),
    "app": (),
}


# ---------------------------------------------------------------------------
# 路径与默认结构
# ---------------------------------------------------------------------------

def settings_path() -> str:
    """配置文件绝对路径：``UITA_DSH_SETTINGS_FILE`` > 用户数据目录（不进仓库）。"""
    override = (os.environ.get("UITA_DSH_SETTINGS_FILE", "") or "").strip()
    if override:
        return os.path.abspath(override)
    from uita import config

    return os.path.join(config.USER_DATA_DIR, FILENAME)


def empty_settings() -> dict[str, Any]:
    return {
        "version": VERSION,
        "llm": {"url": "", "api_key": "", "model": ""},
        "server": {"port": 0, "mode": ""},
        "run": {"max_steps": 0, "timeout_s": 0},
        "defaults": {"app": ""},
    }


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def _as_str(value: Any, limit: int = 512) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def load(path: str | None = None) -> dict[str, Any]:
    """读配置；文件缺失/损坏时返回默认结构（不抛异常，不阻断启动）。"""
    target = path or settings_path()
    data = empty_settings()
    try:
        with open(target, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return data
    except (OSError, ValueError):
        data["error"] = "配置文件无法解析，已按默认值启动"
        return data
    if not isinstance(raw, dict):
        data["error"] = "配置文件内容不是对象，已按默认值启动"
        return data

    llm = _section(raw, "llm")
    server = _section(raw, "server")
    run = _section(raw, "run")
    defaults = _section(raw, "defaults")
    data["llm"] = {
        "url": _as_str(llm.get("url")),
        "api_key": _as_str(llm.get("api_key"), 1024),
        "model": _as_str(llm.get("model"), 128),
    }
    mode = _as_str(server.get("mode"), 32)
    data["server"] = {
        "port": _as_int(server.get("port")),
        "mode": mode if mode in MODES else "",
    }
    data["run"] = {
        "max_steps": _as_int(run.get("max_steps")),
        "timeout_s": _as_int(run.get("timeout_s")),
    }
    app = _as_str(defaults.get("app"), 64)
    data["defaults"] = {"app": app if _APP_RE.match(app) else ""}
    return data


# ---------------------------------------------------------------------------
# 校验与写入
# ---------------------------------------------------------------------------

def _check_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("模型端点只接受 http/https 地址")
    if not parsed.netloc:
        raise ValueError("模型端点缺少主机名（形如 https://host/path/chat/completions）")
    return url


def _check_port(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("端口必须是整数")
    if value == 0:
        return 0
    if not 1 <= value <= 65535:
        raise ValueError("端口取值范围 1–65535（0 表示不覆盖默认值）")
    return value


def _check_ranged(value: Any, *, low: int, high: int, label: str) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}必须是整数")
    if value == 0:
        return 0
    if not low <= value <= high:
        raise ValueError(f"{label}取值范围 {low}–{high}（0 表示不覆盖默认值）")
    return value


def _check_mode(value: Any) -> str:
    if value in (None, ""):
        return ""
    mode = _as_str(value, 32).lower()
    if mode not in MODES:
        raise ValueError(f"模式只能是 {'/'.join(MODES)} 之一")
    return mode


def _check_app(value: Any) -> str:
    app = _as_str(value, 128)
    if len(app) > 64 or not _APP_RE.match(app):
        raise ValueError("应用名只允许字母/数字/下划线/点/连字符，且不超过 64 字符")
    return app


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化整份配置；不合法抛 ``ValueError``（消息可直接回给界面）。"""
    if not isinstance(payload, dict):
        raise ValueError("配置内容必须是 JSON 对象")
    llm = _section(payload, "llm")
    server = _section(payload, "server")
    run = _section(payload, "run")
    defaults = _section(payload, "defaults")
    return {
        "version": VERSION,
        "llm": {
            "url": _check_url(_as_str(llm.get("url"))),
            "api_key": _as_str(llm.get("api_key"), 1024),
            "model": _as_str(llm.get("model"), 128),
        },
        "server": {
            "port": _check_port(server.get("port")),
            "mode": _check_mode(server.get("mode")),
        },
        "run": {
            "max_steps": _check_ranged(run.get("max_steps"), low=1, high=200,
                                       label="最大步数"),
            "timeout_s": _check_ranged(run.get("timeout_s"), low=10, high=7200,
                                       label="会话超时（秒）"),
        },
        "defaults": {"app": _check_app(defaults.get("app"))},
    }


def _write_atomic(target: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".dsh_settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(payload: dict[str, Any], path: str | None = None) -> dict[str, Any]:
    """校验并原子落盘；返回写入后的配置（含明文密钥，仅供进程内使用）。"""
    target = path or settings_path()
    current = load(target)
    candidate = dict(payload or {})
    llm = dict(_section(candidate, "llm"))
    if candidate.get("clear_api_key"):
        llm["api_key"] = ""
    elif not _as_str(llm.get("api_key"), 1024):
        llm["api_key"] = current["llm"]["api_key"]
    candidate["llm"] = llm
    data = normalize(candidate)
    _write_atomic(target, data)
    return data


# ---------------------------------------------------------------------------
# 生效值解析（env > file）
# ---------------------------------------------------------------------------

def env_value(key: str) -> str:
    for name in ENV_KEYS.get(key, ()):
        value = (os.environ.get(name, "") or "").strip()
        if value:
            return value
    return ""


def effective_defaults(path: str | None = None) -> dict[str, Any]:
    """文件值仅在对应环境变量缺省时生效；未配置的数值项返回 0、文本项返回 ''。"""
    st = load(path)

    def pick(key: str, value: Any) -> Any:
        if env_value(key):
            return 0 if isinstance(value, int) else ""
        return value

    return {
        "port": pick("port", st["server"]["port"]),
        "mode": pick("mode", st["server"]["mode"]),
        "max_steps": pick("max_steps", st["run"]["max_steps"]),
        "timeout_s": pick("timeout_s", st["run"]["timeout_s"]),
        "app": pick("app", st["defaults"]["app"]),
        "llm_url": pick("llm_url", st["llm"]["url"]),
        "llm_model": pick("llm_model", st["llm"]["model"]),
        "llm_api_key": pick("llm_api_key", st["llm"]["api_key"]),
    }


def _source(key: str, file_value: Any) -> str:
    if env_value(key):
        return "env"
    return "file" if file_value else "default"


def _with_env(key: str, file_value: Any) -> Any:
    """展示用生效值：env 优先，缺省回落文件值。"""
    raw = env_value(key)
    if not raw:
        return file_value
    if isinstance(file_value, int):
        try:
            return int(raw)
        except ValueError:
            return file_value
    return raw


def public_view(path: str | None = None) -> dict[str, Any]:
    """界面视图：生效值（含 env）+ 来源；密钥只给掩码与「已设置」标记。"""
    target = path or settings_path()
    st = load(target)
    url = _with_env("llm_url", st["llm"]["url"])
    model = _with_env("llm_model", st["llm"]["model"])
    api_key = _with_env("llm_api_key", st["llm"]["api_key"])
    return {
        "path": target,
        "exists": os.path.isfile(target),
        "configured": bool(url and api_key),
        "error": st.get("error", ""),
        "llm": {
            "url": url,
            "model": model,
            "api_key_set": bool(api_key),
            "api_key_mask": MASK if api_key else "",
        },
        "server": {
            "port": _with_env("port", st["server"]["port"]),
            "mode": _with_env("mode", st["server"]["mode"]),
        },
        "run": {
            "max_steps": _with_env("max_steps", st["run"]["max_steps"]),
            "timeout_s": _with_env("timeout_s", st["run"]["timeout_s"]),
        },
        "defaults": {"app": st["defaults"]["app"]},
        "sources": {
            "llm.url": _source("llm_url", st["llm"]["url"]),
            "llm.api_key": _source("llm_api_key", st["llm"]["api_key"]),
            "llm.model": _source("llm_model", st["llm"]["model"]),
            "server.port": _source("port", st["server"]["port"]),
            "server.mode": _source("mode", st["server"]["mode"]),
            "run.max_steps": _source("max_steps", st["run"]["max_steps"]),
            "run.timeout_s": _source("timeout_s", st["run"]["timeout_s"]),
            "defaults.app": _source("app", st["defaults"]["app"]),
        },
    }


def resolve_llm(path: str | None = None) -> dict[str, str]:
    """配置文件侧的模型端点/密钥/模型（不含环境变量，供 planner 兜底）。"""
    st = load(path)
    return {
        "url": st["llm"]["url"],
        "api_key": st["llm"]["api_key"],
        "model": st["llm"]["model"],
    }
