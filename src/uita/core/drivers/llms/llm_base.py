"""
BaseLLM — 视觉 VL 模型调用的公共工具基类。

计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §9（2026-07-03 追加）

设计要点：
  - 使用标准包导入（main.py 已负责把项目根加入 sys.path，不需要各模块自行处理）。
  - `draw_bbox()` 依赖的 cv2/numpy 改为惰性 import（对齐本项目
    core/resolver/l3_8_visual_fingerprint.py 的既有约定：模块本身在未安装
    opencv-python 时仍可正常 import，只有真正调用 draw_bbox 时才会报错）。
  - `encode_image()` 不依赖 cv2，保持随时可用。
"""
from __future__ import annotations

import base64
import os
import re
from abc import ABC
from dataclasses import dataclass


@dataclass
class BBox:
    """千分比坐标的边界框，以及转换后的绝对/相对坐标。"""
    raw: list[int]          # 模型返回的原始 [x1, y1, x2, y2]（千分比，0~1000）
    label: str = ""

    def to_absolute(self, width: int, height: int) -> tuple[tuple[int, int], tuple[int, int]]:
        """千分比坐标 → 绝对像素坐标。"""
        x1 = int(self.raw[0] / 1000 * width)
        y1 = int(self.raw[1] / 1000 * height)
        x2 = int(self.raw[2] / 1000 * width)
        y2 = int(self.raw[3] / 1000 * height)
        return (x1, y1), (x2, y2)

    def to_relative(self, width: int, height: int) -> tuple[tuple[float, float], tuple[float, float]]:
        """千分比坐标 → 0~1 相对坐标。"""
        (x1, y1), (x2, y2) = self.to_absolute(width, height)
        return (x1 / width, y1 / height), (x2 / width, y2 / height)


class BaseLLM(ABC):
    """通用 chat/completions 兼容 VL 模型的抽象基类（提供图片编解码 + bbox 标注 + JSON 提取工具）。"""

    def __init__(self) -> None:
        pass

    @staticmethod
    def _import_cv():
        """惰性 import cv2/numpy，未安装时抛出清晰 RuntimeError。"""
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "draw_bbox 需要 opencv-python + numpy，请先 uv pip install opencv-python numpy"
            ) from exc
        return cv2, np

    @staticmethod
    def encode_image(image_path: str) -> str:
        """将本地图片文件编码为 Base64 字符串。"""
        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"图片文件不存在: {image_path}")
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    @classmethod
    def draw_bbox(
        cls,
        image_path: str,
        bbox: "BBox",
        label: str = "",
        color: tuple[int, int, int] = (0, 0, 255),
        thickness: int = 0,
        show: bool = False,
        save_path: str = "",
    ):
        """在图片上绘制 bbox 矩形框并可选展示/保存。

        Args:
            label: 标注文字，绘制在框右上方（如调用的函数名）。
            thickness: 线条粗细，0 表示根据图像尺寸自动计算。
            save_path: 不为空时将标注后的图片保存到该路径；
                       为空则原地覆盖 image_path。

        Returns:
            (start, end, rel_start, rel_end, annotated_img_ndarray)
        """
        cv2, _np = cls._import_cv()
        img = cv2.imread(image_path)
        h, w = img.shape[:2]

        if thickness <= 0:
            thickness = max(1, min(10, int(min(w, h) * 0.003)))

        start, end = bbox.to_absolute(w, h)
        rel_start, rel_end = bbox.to_relative(w, h)

        cv2.rectangle(img, start, end, color, thickness)

        display_label = label or bbox.label
        if display_label:
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = max(0.4, min(1.2, min(w, h) * 0.0008))
            font_thickness = max(1, int(font_scale * 1.5))
            (tw, th), _ = cv2.getTextSize(display_label, font, font_scale, font_thickness)
            text_x = end[0] + 4
            text_y = start[1] + th + 4
            cv2.rectangle(img, (text_x - 2, text_y - th - 4), (text_x + tw + 2, text_y + 4), color, -1)
            cv2.putText(img, display_label, (text_x, text_y), font, font_scale, (255, 255, 255), font_thickness)

        cv2.imwrite(save_path or image_path, img)

        if show:
            cv2.imshow("bbox_preview", img)
            cv2.waitKey(0)
            cv2.destroyAllWindows()

        return start, end, rel_start, rel_end, img

    @staticmethod
    def _extract_json(text: str) -> str:
        """从模型回复中提取 JSON 字符串（兼容 markdown 代码块与行内噪声）。

        清洗顺序：代码块 → 首个 `{...}` → 去掉行首/值前的 `#` 噪声（部分模型会输出
        `{"ok":##true}`、`{\"ok\":# 真的吗？}` 这类非标准 JSON）。
        """
        if not isinstance(text, str):
            return ""
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = match.group(1) if match else ""
        if not candidate:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            candidate = match.group(0) if match else text
        # 只清 JSON 结构位置上的 `#` 噪声（整行注释、值前 `##true`、结构前 `#`），
        # 不动字符串内容（如 label 里的 "#FF0000"）。
        cleaned = re.sub(r"(?m)^[ \t]*#.*$", "", candidate)
        cleaned = re.sub(r"(:\s*)#+\s*", r"\1", cleaned)
        cleaned = re.sub(r"([{\[,]\s*)#+\s*", r"\1", cleaned)
        return cleaned.strip() or text
