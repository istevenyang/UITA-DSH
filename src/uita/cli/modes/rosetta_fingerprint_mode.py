"""`--mode rosetta-fp`：Rosetta 视觉指纹编码、解码和静态截图匹配。"""
from __future__ import annotations

from uita.cli.modes.common import StartupError
from uita.cli.rosetta_fingerprint import (
    RosettaFingerprintError,
    decode_fingerprint,
    encode_fingerprint,
    match_fingerprint,
)


def _require(value: str, argument: str, fp_mode: str) -> str:
    if not value:
        raise StartupError(f"rosetta-fp {fp_mode} 模式必须指定 {argument}")
    return value


def _handle_rosetta_fingerprint(cli) -> int:
    """统一 CLI handler；业务实现位于 `cli.rosetta_fingerprint`。"""
    fp_mode = getattr(cli, "fp_mode", "")
    if fp_mode not in {"encode", "decode", "match"}:
        raise StartupError("rosetta-fp 模式必须指定 --fp-mode encode|decode|match")

    app = _require((getattr(cli, "app", "") or "").strip(), "--app", fp_mode)
    element = _require((getattr(cli, "element", "") or "").strip(), "--element", fp_mode)
    platform = getattr(cli, "fp_platform", None)
    index = getattr(cli, "fp_index", 0)

    try:
        if fp_mode == "encode":
            pattern = _require((getattr(cli, "fp_pattern", "") or "").strip(), "--pattern", fp_mode)
            result = encode_fingerprint(
                app=app,
                element=element,
                pattern_path=pattern,
                page=(getattr(cli, "fp_page", "") or "").strip(),
                usage=(getattr(cli, "fp_usage", "") or "").strip(),
                platform=platform,
                append=getattr(cli, "fp_append", False),
            )
            print(result.encoded)
            print(
                f"[rosetta-fp] {result.action}成功: app={app} element={element!r} "
                f"field={result.field_name} map_path={result.map_path}"
            )
            return 0

        if fp_mode == "decode":
            result = decode_fingerprint(app=app, element=element, platform=platform, index=index)
            print(f"[rosetta-fp] 解码成功 → {result.output_path} ({result.width}x{result.height})")
            return 0

        screenshot = _require((getattr(cli, "fp_screenshot", "") or "").strip(), "--screenshot", fp_mode)
        result = match_fingerprint(
            app=app, element=element, screenshot_path=screenshot, platform=platform, index=index,
        )
        print(f"[rosetta-fp] 模板指纹图已输出 → {result.template_path}")
        if not result.matched:
            print("[rosetta-fp] 未命中：视觉指纹与截图无法匹配（confidence 低于阈值）")
            return 0
        x, y, width, height = result.bbox or (0, 0, 0, 0)
        print(
            f"[rosetta-fp] 命中: confidence={result.confidence:.3f} algo={result.algorithm} "
            f"bbox=({x},{y},{width},{height}) click_center={result.click_center}"
        )
        print(f"[rosetta-fp] 标注截图已输出 → {result.annotated_path}")
        return 0
    except RosettaFingerprintError as exc:
        raise StartupError(str(exc)) from exc
