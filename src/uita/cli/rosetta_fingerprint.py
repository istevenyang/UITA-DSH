"""Rosetta 视觉指纹的可复用编解码与匹配服务。

本模块属于 CLI 编排层：可调用 core 的指纹编解码/匹配能力，但不向 core 引入
Pillow、OpenCV 或 argparse 依赖。`tools/rosetta/rosetta_fingerprint_cli.py` 与
`--mode rosetta-fp` 共用本模块，防止两套 CLI 语义漂移。
"""
from __future__ import annotations

import json
import os
import platform as host_platform
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterator

from uita.core.platform.element_map_loader import _looks_like_element
from uita.core.rosetta.pattern_codec import decode as pattern_decode
from uita.core.rosetta.pattern_codec import encode as pattern_encode
from uita.config import REPORT_DIR, WORKSPACE_DIR

_PLATFORM_FIELD_MAP = {"mac": "pattern_mac", "win": "pattern_win", "linux": "pattern_linux"}
_CLICK_COLOR = "#FF3B30"


class RosettaFingerprintError(Exception):
    """用户可修正的 Rosetta 视觉指纹操作错误。"""


@dataclass(frozen=True)
class EncodeResult:
    encoded: str
    action: str
    field_name: str
    map_path: str


@dataclass(frozen=True)
class DecodeResult:
    output_path: str
    width: int
    height: int


@dataclass(frozen=True)
class MatchResult:
    template_path: str
    matched: bool
    confidence: float | None = None
    algorithm: str | None = None
    bbox: tuple[int, int, int, int] | None = None
    click_center: tuple[int, int] | None = None
    annotated_path: str | None = None


def _validate_app_name(app: str) -> str:
    name = (app or "").strip()
    if not name:
        raise RosettaFingerprintError("rosetta-fp 模式必须指定 --app")
    if name in {".", ".."} or os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        raise RosettaFingerprintError(f"非法 --app: {app!r}（只允许 apps/ 下的单层目录名）")
    return name


def _rosetta_map_path(app: str) -> str:
    app_name = _validate_app_name(app)
    # apps/ 为业务资产（WORKSPACE_DIR），非框架包安装位置，pip 场景下二者不同。
    path = os.path.join(WORKSPACE_DIR, "apps", app_name, "rosetta_map.json")
    if not os.path.isfile(path):
        raise RosettaFingerprintError(
            f"未找到 app={app_name!r} 的 rosetta_map.json（路径不存在: {path}），请检查 --app 拼写"
        )
    return path


def _load_map(map_path: str) -> dict:
    try:
        with open(map_path, encoding="utf-8") as file:
            raw = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RosettaFingerprintError(f"读取 rosetta_map.json 失败: {map_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RosettaFingerprintError(f"rosetta_map.json 根节点必须是对象: {map_path}")
    return raw


def _detect_indent(map_path: str, default: int = 2) -> int:
    """探测现有 rosetta_map 的缩进宽度（首层键前的空格数），默认 2。

    为什么必须探测：rosetta_map 是**人也在手改**的正式配置，写盘缩进与文件既有风格
    不一致会把整文件重排（上千行 diff），真实改动被淹没在格式噪音里。
    """
    try:
        with open(map_path, encoding="utf-8") as file:
            for line in file:
                stripped = line.lstrip(" ")
                if stripped.startswith('"') or stripped.startswith("}"):
                    width = len(line) - len(stripped)
                    return width or default
    except (OSError, UnicodeDecodeError):
        return default
    return default


def _write_map_atomically(map_path: str, raw: dict) -> None:
    """同目录写入临时 JSON 后原子替换，避免中断时截断正式 rosetta_map。"""
    directory = os.path.dirname(map_path)
    indent = _detect_indent(map_path)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".rosetta_map.", suffix=".tmp", delete=False
        ) as file:
            temp_path = file.name
            json.dump(raw, file, ensure_ascii=False, indent=indent)
            file.write("\n")
        os.replace(temp_path, map_path)
    except OSError as exc:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise RosettaFingerprintError(f"原子写入 rosetta_map.json 失败: {map_path}: {exc}") from exc


def _iter_elements(raw: dict) -> Iterator[tuple[str, str | None, str, dict]]:
    """遍历树形/平铺 RosettaMap 中的元素字段字典。"""
    for root_key, root_value in raw.items():
        if root_key.startswith("_") or not isinstance(root_value, dict):
            continue
        if _looks_like_element(root_value):
            yield root_key, None, root_key, root_value
            continue
        for page_name, page_data in root_value.items():
            if not isinstance(page_data, dict):
                continue
            for element_name, fields in page_data.items():
                if isinstance(fields, dict):
                    yield root_key, page_name, element_name, fields


def _find_element(raw: dict, element_name: str) -> dict | None:
    for _root_key, _page_name, current_name, fields in _iter_elements(raw):
        if current_name == element_name:
            return fields
    return None


def _detect_platform_field(explicit: str | None) -> str:
    if explicit:
        key = explicit.strip().lower()
        if key not in _PLATFORM_FIELD_MAP:
            raise RosettaFingerprintError(f"非法 --platform 取值: {explicit!r}，合法取值: mac/win/linux")
        return _PLATFORM_FIELD_MAP[key]
    mapped = {"Darwin": "mac", "Windows": "win", "Linux": "linux"}.get(host_platform.system())
    return _PLATFORM_FIELD_MAP[mapped] if mapped else "pattern"


def _resolve_pattern_value(fields: dict, field_name: str, index: int) -> str:
    raw = fields.get(field_name)
    resolved_field = field_name
    if not raw and field_name != "pattern":
        raw = fields.get("pattern")
        resolved_field = "pattern"
    if not raw:
        raise RosettaFingerprintError(f"该元素没有可用的视觉指纹字段（已尝试 {field_name}/pattern）")
    if isinstance(raw, str):
        if index != 0:
            raise RosettaFingerprintError(f"字段 {resolved_field!r} 只有 1 张指纹（字符串），--index 只能为 0")
        return raw
    if isinstance(raw, list):
        if not raw:
            raise RosettaFingerprintError(f"字段 {resolved_field!r} 是空数组，没有可用指纹")
        if index < 0 or index >= len(raw):
            raise RosettaFingerprintError(f"字段 {resolved_field!r} 共有 {len(raw)} 张指纹，--index={index} 越界")
        value = raw[index]
        if isinstance(value, str):
            return value
    raise RosettaFingerprintError(f"字段 {resolved_field!r} 类型非法（应为字符串或字符串数组）")


def _read_bytes(path: str, label: str) -> bytes:
    if not path or not os.path.isfile(path):
        raise RosettaFingerprintError(f"{label}不存在: {path}")
    try:
        with open(path, "rb") as file:
            return file.read()
    except OSError as exc:
        raise RosettaFingerprintError(f"读取{label}失败: {path}: {exc}") from exc


def _fingerprint_out_dir() -> str:
    # 运行时产物落 REPORT_DIR（USER_DATA_DIR 下），而非 ROOT_DIR/WORKSPACE_DIR。
    out_dir = os.path.join(REPORT_DIR, "fingerprint")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _write_bytes(path: str, content: bytes) -> None:
    try:
        with open(path, "wb") as file:
            file.write(content)
    except OSError as exc:
        raise RosettaFingerprintError(f"写入文件失败: {path}: {exc}") from exc


def encode_fingerprint(
    *, app: str, element: str, pattern_path: str, page: str = "", usage: str = "",
    platform: str | None = None, append: bool = False,
) -> EncodeResult:
    """将 PNG 无损编码后写入指定元素的指纹字段。"""
    if not element:
        raise RosettaFingerprintError("rosetta-fp 模式必须指定 --element")
    map_path = _rosetta_map_path(app)
    png_bytes = _read_bytes(pattern_path, "指纹 PNG 文件")
    raw = _load_map(map_path)
    fields = _find_element(raw, element)
    field_name = _detect_platform_field(platform)

    if fields is None:
        if not page or not usage:
            raise RosettaFingerprintError(
                f"元素 {element!r} 在 {map_path} 中不存在，新建元素需同时提供 --page 和 --usage"
            )
        app_name = _validate_app_name(app)
        fields = {"usage": usage}
        raw.setdefault(app_name, {}).setdefault(page, {})[element] = fields
        action = "新建"
    else:
        action = "更新"

    encoded = pattern_encode(png_bytes)
    if append and fields.get(field_name):
        existing = fields[field_name]
        fields[field_name] = [*existing, encoded] if isinstance(existing, list) else [existing, encoded]
    else:
        fields[field_name] = encoded
    _write_map_atomically(map_path, raw)
    return EncodeResult(encoded=encoded, action=action, field_name=field_name, map_path=map_path)


def _load_decoded_pattern(*, app: str, element: str, platform: str | None, index: int) -> tuple[bytes, str]:
    if not element:
        raise RosettaFingerprintError("rosetta-fp 模式必须指定 --element")
    map_path = _rosetta_map_path(app)
    fields = _find_element(_load_map(map_path), element)
    if fields is None:
        raise RosettaFingerprintError(f"元素 {element!r} 在 {map_path} 中不存在")
    try:
        return pattern_decode(_resolve_pattern_value(fields, _detect_platform_field(platform), index)), map_path
    except Exception as exc:
        if isinstance(exc, RosettaFingerprintError):
            raise
        raise RosettaFingerprintError(f"视觉指纹解码失败: {exc}") from exc


def _image_dimensions(png_bytes: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RosettaFingerprintError("decode/match 需要 Pillow，请先安装项目图像依赖") from exc
    try:
        with Image.open(BytesIO(png_bytes)) as image:
            return image.width, image.height
    except Exception as exc:
        raise RosettaFingerprintError(f"指纹 PNG 解码失败: {exc}") from exc


def decode_fingerprint(*, app: str, element: str, platform: str | None = None, index: int = 0) -> DecodeResult:
    """从 RosettaMap 解码一张视觉指纹到 `report/fingerprint/`。"""
    png_bytes, _map_path = _load_decoded_pattern(app=app, element=element, platform=platform, index=index)
    width, height = _image_dimensions(png_bytes)
    output_path = os.path.join(_fingerprint_out_dir(), f"{element}.png")
    _write_bytes(output_path, png_bytes)
    return DecodeResult(output_path=output_path, width=width, height=height)


def _draw_match_marker(src_path: str, dst_path: str, match) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RosettaFingerprintError("match 生成标注图需要 Pillow，请先安装项目图像依赖") from exc
    try:
        with Image.open(src_path) as source:
            image = source.convert("RGB")
        draw = ImageDraw.Draw(image)
        bbox = match.sub_bbox if match.sub_bbox is not None else (match.x, match.y, match.width, match.height)
        bx, by, bw, bh = bbox
        border = max(2, round(image.width / 700))
        draw.rectangle([bx - 1, by - 1, bx + bw + 1, by + bh + 1], outline="#FFFFFF", width=1)
        draw.rectangle([bx, by, bx + bw, by + bh], outline=_CLICK_COLOR, width=border)
        cx, cy = match.click_center
        radius = max(4, border * 2)
        draw.line([cx - radius, cy, cx + radius, cy], fill=_CLICK_COLOR, width=border)
        draw.line([cx, cy - radius, cx, cy + radius], fill=_CLICK_COLOR, width=border)
        label = f"confidence={match.confidence:.2f} algo={match.match_algo}"
        label_y = max(0, by - 20)
        draw.rectangle([bx, label_y, bx + 8 * len(label) + 8, label_y + 18], fill=_CLICK_COLOR)
        draw.text((bx + 4, label_y + 2), label, fill="#FFFFFF")
        image.save(dst_path)
    except RosettaFingerprintError:
        raise
    except Exception as exc:
        raise RosettaFingerprintError(f"生成匹配标注图失败: {exc}") from exc


def match_fingerprint(
    *, app: str, element: str, screenshot_path: str, platform: str | None = None, index: int = 0,
) -> MatchResult:
    """用既有视觉指纹匹配一张静态截图，命中时输出带标注的截图拷贝。"""
    template_png_bytes, _map_path = _load_decoded_pattern(app=app, element=element, platform=platform, index=index)
    out_dir = _fingerprint_out_dir()
    template_path = os.path.join(out_dir, f"{element}.png")
    _write_bytes(template_path, template_png_bytes)
    screenshot_bytes = _read_bytes(screenshot_path, "页面截图")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RosettaFingerprintError("match 需要 opencv-python 与 numpy，请先安装项目图像依赖") from exc

    screenshot = cv2.imdecode(np.frombuffer(screenshot_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if screenshot is None:
        raise RosettaFingerprintError(f"截图解码失败（非有效图片）: {screenshot_path}")

    from uita.core.resolver.l7_visual_fingerprint import VisualFingerprintResolver

    with tempfile.TemporaryDirectory() as temp_dir:
        resolver = VisualFingerprintResolver(app_name="tool", platform="adhoc", fingerprint_dir=temp_dir)
        template_dir = Path(temp_dir) / "tool_adhoc"
        template_dir.mkdir(parents=True, exist_ok=True)
        (template_dir / f"{element}.png").write_bytes(template_png_bytes)
        match = resolver.match(element, screenshot)

    if match is None:
        return MatchResult(template_path=template_path, matched=False)

    annotated_path = os.path.join(out_dir, os.path.basename(screenshot_path))
    _draw_match_marker(screenshot_path, annotated_path, match)
    return MatchResult(
        template_path=template_path,
        matched=True,
        confidence=match.confidence,
        algorithm=match.match_algo,
        bbox=(match.x, match.y, match.width, match.height),
        click_center=match.click_center,
        annotated_path=annotated_path,
    )
