"""Trusted, explicitly injected read-only host; no implicit model or safety grant."""
from __future__ import annotations

import inspect
import json
import math
import tempfile
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .evidence import EvidenceWriter
from .observation import validate_observation
from .planner import JSONPlanner
from .tcs_adapter import TCSFailureReviewer
from .runtime import AgentRuntime
from .session import Config, Failure, Mode, Observation, Safety


class TextPlanningClient(Protocol):
    async def chat(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class WindowSnapshot:
    summary: str
    targets: tuple[str, ...]
    window_title: str
    window_rect: tuple[float, float, float, float]
    accessibility: Any = None
    screenshot: bytes | None = None
    product_mismatch: bool = False


HostContext = Mapping[str, Any]
ObservationProvider = Callable[[HostContext, Failure], Awaitable[WindowSnapshot]]
SafetyProvider = Callable[[HostContext, Failure], Awaitable[Safety]]


def _rect(value: Any) -> tuple[float, float, float, float]:
    if (not isinstance(value, (tuple, list)) or len(value) != 4
            or any(type(item) not in (int, float) or not math.isfinite(item) for item in value)
            or value[2] <= 0 or value[3] <= 0):
        raise ValueError("current window rectangle required")
    return tuple(value)


def _safe_root(collector: Any) -> Path:
    value = getattr(collector, "result_dir", None)
    if not isinstance(value, str) or not value:
        raise ValueError("case result directory required")
    root = Path(value).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("result directory must not traverse symbolic links")
    return root


def _context(context: dict[str, Any], root: Path, screenshot: Path) -> HostContext:
    return MappingProxyType({
        "engine": context.get("engine"), "app_name": context.get("app_name", ""),
        "case_file": context.get("case_file", ""), "case_name": context.get("case_name", ""),
        "step_index": context["step_index"], "step": deepcopy(context["step"]),
        "result_dir": str(root), "screenshot_path": str(screenshot),
    })


def _observed_targets(value: Any, title: str) -> tuple[str, ...]:
    targets = [title]

    def visit(node: Any) -> None:
        if len(targets) >= 64:
            return
        if isinstance(node, dict):
            for key in ("name", "text", "automation_id", "automationId", "id"):
                item = node.get(key)
                if isinstance(item, str) and 0 < len(item) <= 1024 and item not in targets:
                    targets.append(item)
                    if len(targets) >= 64:
                        return
            for item in node.values():
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return tuple(targets)


class EngineObservationProvider:
    """Use verified EngineBase APIs; unsupported accessibility fails closed.

    Only fresh, confirmed window captures are returned. Screenshots are optional;
    the existing planning client accepts text, not images.
    """

    def __init__(self, *, capture_screenshot: bool = False):
        if type(capture_screenshot) is not bool:
            raise ValueError("capture_screenshot must be bool")
        self.capture_screenshot = capture_screenshot

    async def __call__(self, context: HostContext, failure: Failure) -> WindowSnapshot:
        engine = context.get("engine")
        required = ("refresh_active_window", "get_window_title", "get_window_rect",
                    "get_accessibility_summary")
        if any(not callable(getattr(engine, name, None)) for name in required):
            raise ValueError("engine lacks current-window observation capability")
        await engine.refresh_active_window()
        title = await engine.get_window_title()
        rect = _rect(await engine.get_window_rect())
        summary = await engine.get_accessibility_summary(max_nodes=200)
        if (not isinstance(title, str) or not 0 < len(title) <= 1024
                or not isinstance(summary, str) or not summary.strip() or len(summary) > 65536):
            raise ValueError("current accessibility summary and window identity required")
        try:
            accessibility = json.loads(summary)
        except ValueError:
            accessibility = summary
        image = None
        if self.capture_screenshot:
            capture = getattr(engine, "take_screenshot", None)
            if not callable(capture):
                raise ValueError("engine lacks screenshot capability")
            destination = Path(context["screenshot_path"])
            if destination.exists() or destination.is_symlink():
                raise ValueError("screenshot path must be fresh")
            data = await capture(str(destination))
            capture_rect_provider = getattr(engine, "get_last_screenshot_rect", None)
            capture_rect = (await capture_rect_provider()
                            if callable(capture_rect_provider) else None)
            if (type(data) is bytes and data and destination.is_file()
                    and not destination.is_symlink()
                    and destination.stat().st_nlink == 1
                    and destination.read_bytes() == data
                    and getattr(engine, "_last_screenshot_is_window", False) is True
                    and capture_rect is not None and _rect(capture_rect) == rect):
                image = data
            if destination.is_symlink():
                raise ValueError("unsafe screenshot artifact")
            if destination.exists() and image is None:
                destination.unlink()
        if (await engine.get_window_title() != title
                or _rect(await engine.get_window_rect()) != rect):
            raise ValueError("active window changed during observation")
        return WindowSnapshot(summary[:8192], _observed_targets(accessibility, title),
                              title, rect, accessibility, image)


class _HostObserver:
    def __init__(self, context: dict[str, Any], provider: ObservationProvider,
                 safety_provider: SafetyProvider):
        self._context = context
        self._provider = provider
        self._safety_provider = safety_provider
        self._collector = context["collector"]
        self._root = _safe_root(self._collector)

    async def __call__(self, failure: Failure) -> Observation:
        root = _safe_root(self._collector)
        if (root != self._root or self._context.get("collector") is not self._collector
                or str(root.parent) != failure.run_id
                or (self._context.get("case_file") or self._context.get("case_name")) != failure.case_id):
            raise ValueError("observation belongs to another case or result directory")
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="agent-observation-", dir=root))
        screenshot = directory / "window.png"
        context = _context(self._context, root, screenshot)

        def validate_directory():
            if (_safe_root(self._collector) != root or directory.is_symlink()
                    or not directory.is_dir() or directory.resolve().parent != root.resolve()):
                raise ValueError("observation artifact directory changed")

        safety = await self._safety_provider(context, failure)
        validate_directory()
        if not isinstance(safety, Safety):
            raise ValueError("explicit host safety evidence required")
        if not safety.valid:
            snapshot = None
            observation = Observation(failure.case_id, "required safety preconditions failed",
                                      (directory.relative_to(root).as_posix() + "/observation.json",),
                                      (), safety)
        else:
            snapshot = await self._provider(context, failure)
            validate_directory()
            if not isinstance(snapshot, WindowSnapshot):
                raise ValueError("structured current-window snapshot required")
            _rect(snapshot.window_rect)
            if not isinstance(snapshot.window_title, str) or not 0 < len(snapshot.window_title) <= 1024:
                raise ValueError("current window identity required")
            refs = [directory.relative_to(root).as_posix() + "/observation.json"]
            if snapshot.screenshot is not None:
                if (type(snapshot.screenshot) is not bytes or len(snapshot.screenshot) > 20971520
                        or not snapshot.screenshot.startswith(b"\x89PNG\r\n\x1a\n")):
                    raise ValueError("window screenshot must be PNG bytes")
                if screenshot.is_symlink():
                    raise ValueError("unsafe screenshot artifact")
                if not screenshot.exists():
                    with screenshot.open("xb") as stream:
                        stream.write(snapshot.screenshot)
                if (screenshot.stat().st_nlink != 1
                        or screenshot.read_bytes() != snapshot.screenshot):
                    raise ValueError("screenshot does not match current window snapshot")
                refs.append(screenshot.relative_to(root).as_posix())
            observation = Observation(failure.case_id, snapshot.summary, tuple(refs),
                                      snapshot.targets, safety, snapshot.product_mismatch)
        validate_observation(failure, observation)
        validate_directory()
        payload = {"version": "1.0", "failure": asdict(failure), "captured_ns": time.time_ns(),
                   "observation": asdict(observation),
                   "window": None if snapshot is None else {
                       "title": snapshot.window_title, "rect": snapshot.window_rect,
                       "accessibility": snapshot.accessibility,
                       "screenshot_ref": None if snapshot.screenshot is None else refs[-1],
                   }}
        content = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(content) > 131072:
            raise ValueError("observation artifact too large")
        destination = directory / "observation.json"
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(content)
        for index, ref in enumerate(observation.evidence_refs):
            self._collector.add_artifact(f"failure_observation_{directory.name}_{index}", ref)
        return observation


def make_read_only_reviewer(
    client: TextPlanningClient, observation_provider: ObservationProvider,
    safety_provider: SafetyProvider,
    verification_evidence: Callable[[dict[str, Any], Any], tuple[str, ...]], *,
    config: Config = Config(mode=Mode.READ_ONLY),
) -> TCSFailureReviewer:
    """Compose a trusted host using the existing async text-chat contract.

    Providers are mandatory and receive an isolated, read-only context, without
    run_step or collector capabilities. The original TCS verifier retains PASS
    authority. RECOVERY and automatic approval/map writes are not exposed.
    """
    if not isinstance(config, Config):
        raise ValueError("explicit runtime configuration required")
    if config.mode is Mode.OFF:
        return TCSFailureReviewer(config=config)
    if config.mode is not Mode.READ_ONLY:
        raise ValueError("host permits only read-only review")
    chat = getattr(client, "chat", None)
    if not callable(chat) or not inspect.iscoroutinefunction(chat):
        raise ValueError("client must implement async chat(prompt)")
    try:
        inspect.signature(chat).bind("prompt")
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported chat signature; inject an explicit text adapter") from exc
    if any(not callable(item) for item in (observation_provider, safety_provider, verification_evidence)):
        raise ValueError("observation, safety and verification evidence providers are mandatory")
    planner = JSONPlanner.from_client(client)

    def factory(context, failure, verify):
        return AgentRuntime(config=config,
                            observe=_HostObserver(context, observation_provider, safety_provider),
                            plan=planner, verify=verify, evidence=EvidenceWriter(context["collector"]))

    return TCSFailureReviewer(runtime_factory=factory, verification_evidence=verification_evidence,
                              config=config)
