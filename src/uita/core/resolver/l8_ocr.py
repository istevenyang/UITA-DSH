"""
L8 OCR 文字识别 — assert/verify/if/elif 存在性判定辅助层。

子层顺序体系：
  L7  视觉指纹存在性断言（仅 assert/verify 触发）
  L8  本层，OCR 文字识别（assert/verify/if/elif 均触发）

计划文档（OCR 识别逻辑本身，未改动）：
docs/plans/archive/03-resolver-rosetta-实施纪要.md §3.3

平台原生 OCR 优先：
  macOS  : Vision.framework（VNRecognizeTextRequest，离线，< 200ms）
  通用兜底: pytesseract（需预装 tesseract）

仅用于 assert/verify/if/elif 的存在性判定，不用于 click（OCR 只给文字区域坐标，
不给可交互元素句柄）。

依赖说明（惰性 import）：pyobjc/pytesseract 均为可选依赖，模块 import 本身
不需要它们已安装，只有真正调用时才会 import。

架构：与 L7 assert_fingerprint / L8 LLM 并列注入
`ExistenceJudger.fallback_chain`，由 `core/runner/resolver_factory.py::
build_resolver_for_app()` 调用 `inject_l8_ocr_fallback()` 完成注入。
"""
from __future__ import annotations

import logging
import platform
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# 模块级标签，与 L7（_uita_assert_fp_layer）、L8（_uita_l8_layer）对齐。
_L8_OCR_TAG = "_uita_l8_ocr_layer"

# OCR 命中所需的最低置信度阈值。
DEFAULT_OCR_CONFIDENCE_THRESHOLD = 0.5


@dataclass
class OcrMatch:
    """OCR 匹配结果。"""
    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)


async def ocr_find_text(screenshot: bytes, target_text: str) -> Optional[OcrMatch]:
    """在截图中查找目标文字，返回匹配区域坐标或 None。

    优先使用 macOS Vision.framework，失败时降级到 pytesseract。

    Args:
        screenshot: PNG 格式截图字节
        target_text: 要查找的目标文字（模糊匹配：目标文字是 OCR 结果的子串即命中）

    Returns:
        OcrMatch 或 None
    """
    if not screenshot or not target_text:
        return None

    if platform.system() == "Darwin":
        result = await _ocr_vision_framework(screenshot, target_text)
        if result is not None:
            return result

    result = await _ocr_pytesseract(screenshot, target_text)
    return result


async def _ocr_vision_framework(screenshot: bytes, target_text: str) -> Optional[OcrMatch]:
    """macOS Vision.framework OCR（离线，快速）。"""
    try:
        import objc  # noqa: F401
        import Quartz
        from Quartz import (
            CGDataProviderCreateWithCFData,
            CGImageCreateWithPNGDataProvider,
            CGColorSpaceCreateDeviceRGB,
        )
        from Foundation import NSData
        import Vision

        ns_data = NSData.dataWithBytes_length_(screenshot, len(screenshot))
        data_provider = CGDataProviderCreateWithCFData(ns_data)
        cg_image = CGImageCreateWithPNGDataProvider(
            data_provider, None, True, 0
        )
        if not cg_image:
            return None

        img_width = Quartz.CGImageGetWidth(cg_image)
        img_height = Quartz.CGImageGetHeight(cg_image)

        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(["zh-Hans", "en"])
        request.setUsesLanguageCorrection_(True)

        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            cg_image, None
        )
        success = handler.performRequests_error_([request], None)
        if not success[0]:
            return None

        results = request.results()
        if not results:
            return None

        for observation in results:
            recognized = observation.topCandidates_(1)
            if not recognized:
                continue
            text = recognized[0].string()
            if target_text in text:
                bbox = observation.boundingBox()
                x = int(bbox.origin.x * img_width)
                y = int((1 - bbox.origin.y - bbox.size.height) * img_height)
                w = int(bbox.size.width * img_width)
                h = int(bbox.size.height * img_height)
                return OcrMatch(
                    text=text,
                    x=x, y=y, width=w, height=h,
                    confidence=float(recognized[0].confidence()),
                )

        return None
    except ImportError:
        logger.debug("[L8][OCR] Vision.framework 不可用（pyobjc 未安装）")
        return None
    except Exception as exc:
        logger.debug("[L8][OCR] Vision.framework OCR 异常: %s", exc)
        return None


async def _ocr_pytesseract(screenshot: bytes, target_text: str) -> Optional[OcrMatch]:
    """pytesseract 兜底 OCR。"""
    try:
        import pytesseract
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(screenshot))
        data = pytesseract.image_to_data(
            img, lang="chi_sim+eng", output_type=pytesseract.Output.DICT
        )

        n = len(data["text"])
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            if target_text in text:
                return OcrMatch(
                    text=text,
                    x=data["left"][i],
                    y=data["top"][i],
                    width=data["width"][i],
                    height=data["height"][i],
                    confidence=float(data["conf"][i]) / 100.0,
                )

        # 尝试连续文本合并匹配（相邻文字块拼接）
        buffer_text = ""
        buffer_start = -1
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                buffer_text = ""
                buffer_start = -1
                continue
            if buffer_start < 0:
                buffer_start = i
            buffer_text += text
            if target_text in buffer_text:
                x = data["left"][buffer_start]
                y = data["top"][buffer_start]
                x_end = data["left"][i] + data["width"][i]
                y_end = max(data["top"][j] + data["height"][j] for j in range(buffer_start, i + 1))
                return OcrMatch(
                    text=buffer_text,
                    x=x, y=y,
                    width=x_end - x, height=y_end - y,
                    confidence=0.7,
                )

        return None
    except ImportError:
        logger.debug("[L8][OCR] pytesseract 不可用")
        return None
    except Exception as exc:
        logger.debug("[L8][OCR] pytesseract OCR 异常: %s", exc)
        return None


def inject_l8_ocr_fallback() -> None:
    """向 ExistenceJudger.fallback_chain 注入 L8 OCR 存在性检查。

    注入位置：无条件插入链首（`chain.insert(0, fn)`）。调用方须保证
    **先调用本函数、再调用 `inject_assert_fingerprint_fallback()`**，才能让
    L7 排在本层之前（两者注入逻辑都是插链首，后调用者更靠前）。
    """
    from uita.core.actions.judgers.existence import ExistenceJudger

    if any(getattr(fn, _L8_OCR_TAG, False) for fn in ExistenceJudger.fallback_chain):
        logger.debug("[L8][OCR] 已注入，跳过")
        return

    async def _l8_ocr_check_fn(ctx, description: str) -> Optional[bool]:
        try:
            screenshot = await ctx.engine.take_screenshot()
        except Exception as exc:
            logger.debug("[L8][OCR] take_screenshot 异常（忽略）: %s", exc)
            return None
        if not screenshot:
            return None

        match = await ocr_find_text(screenshot, description)
        if match and match.confidence >= DEFAULT_OCR_CONFIDENCE_THRESHOLD:
            try:
                ctx.extra["existence_strategy"] = "ocr"
            except Exception:
                pass
            return True
        return None

    setattr(_l8_ocr_check_fn, _L8_OCR_TAG, True)
    ExistenceJudger.fallback_chain = [_l8_ocr_check_fn, *ExistenceJudger.fallback_chain]
    logger.info("[L8][OCR] 已注入 ExistenceJudger.fallback_chain（链首）")
