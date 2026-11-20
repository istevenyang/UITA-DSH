"""
pattern_codec — rosetta_map.json 中 `pattern` 字段的 z85 压缩编解码工具（P4 新增）。

计划文档：
  - docs/plans/archive/03-resolver-rosetta-实施纪要.md §3.2（原始设计）
  - docs/plans/archive/03-resolver-rosetta-实施纪要.md（本次无损化修复）
参考设计：docs/uita-framework.md §2.2.8b（pattern 字段压缩方案）

编码格式（新旧三态，decode() 按前缀自动分派，互相兼容）：
  - "z85zl:<原始字节长度>:<z85 正文>"（当前默认格式）：zlib 无损压缩后再 z85 编码。
  - "z85:<原始字节长度>:<z85 正文>"（历史格式，仍可读）：z85 编码，无压缩层。
  - 无前缀（最旧格式，仍可读）：纯 base64（向后兼容手工写入的旧数据）。
z85/base64 均要求输入长度为 4 的整数倍，故编码时先补零填充，解码时按记录的
原始字节长度截断丢弃填充部分。

z85（ZeroMQ RFC 32）比标准 base64 编码密度更高（85 进制 vs 64 进制），每 4
字节编码为 5 个 ASCII 字符（base64 每 3 字节编码为 4 字符）。zlib 是 Python
标准库自带的无损压缩算法（严格数学可逆，不存在精度损失），叠加在 z85 之前
可进一步压缩字符串长度（对已是 PNG 格式的小图实测仍可再省 ~10%~15%）。

本模块零外部依赖（z85 编解码、zlib 均为标准库/纯 Python 实现）；可选的裁剪
功能（bbox）依赖 Pillow，未安装时静默跳过，仍能正常编码原图（不因缺少可选
依赖而导致核心编解码往返失败）。

**默认无损**：encode() 默认不做任何有损缩放（max_side=0），配合 bbox 精确
像素裁剪（无插值）与 zlib/z85 的严格可逆变换，encode()→decode() 全链路
应当恰好还原为裁剪后的原始字节（100% 匹配，见 image_similarity()）。
"""
from __future__ import annotations

import base64
import logging
import zlib
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)

# Z85（ZeroMQ RFC 32）标准 85 字符字母表
_Z85_ALPHABET = (
    "0123456789abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ.-:+=^!/*?&<>()[]{}@%$#"
)
_Z85_DECODE_MAP = {c: i for i, c in enumerate(_Z85_ALPHABET)}


def _z85_encode(data: bytes) -> str:
    if len(data) % 4 != 0:
        raise ValueError("z85 编码要求输入长度为 4 的整数倍（调用方需先补零填充）")
    chars = []
    for i in range(0, len(data), 4):
        value = int.from_bytes(data[i:i + 4], "big")
        chunk = [""] * 5
        for j in range(4, -1, -1):
            chunk[j] = _Z85_ALPHABET[value % 85]
            value //= 85
        chars.append("".join(chunk))
    return "".join(chars)


def _z85_decode(text: str) -> bytes:
    if len(text) % 5 != 0:
        raise ValueError("z85 解码要求输入长度为 5 的整数倍")
    out = bytearray()
    for i in range(0, len(text), 5):
        value = 0
        for ch in text[i:i + 5]:
            try:
                value = value * 85 + _Z85_DECODE_MAP[ch]
            except KeyError as exc:
                raise ValueError(f"z85 解码遇到非法字符: {ch!r}") from exc
        out += value.to_bytes(4, "big")
    return bytes(out)


def _maybe_crop_and_resize(image_bytes: bytes, bbox: Optional[tuple], max_side: int) -> bytes:
    """可选的裁剪/缩放（依赖 Pillow）。未安装 Pillow 或处理失败时原样返回。

    bbox 裁剪是精确像素矩形切片（无插值），天然无损。max_side 缩放（当
    max_side > 0 且图像最长边超过该值时触发）是有损插值降采样，默认
    max_side=0 表示不缩放（见 encode() 默认参数，2026-07-31 起默认无损）。
    """
    if bbox is None and (not max_side or max_side <= 0):
        return image_bytes
    try:
        from PIL import Image
    except ImportError:
        logger.debug("[pattern_codec] Pillow 未安装，跳过裁剪/缩放，直接编码原图")
        return image_bytes
    try:
        img = Image.open(BytesIO(image_bytes))
        if bbox is not None:
            img = img.crop(bbox)
        if max_side and max(img.size) > max_side:
            ratio = max_side / max(img.size)
            new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
            img = img.resize(new_size)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        logger.debug("[pattern_codec] 裁剪/缩放失败（忽略，回退原图编码）: %s", exc)
        return image_bytes


def encode(
    image_bytes: bytes,
    bbox: Optional[tuple] = None,
    max_side: int = 0,
    compress: bool = True,
) -> str:
    """将图像字节编码为 z85 字符串。

    Args:
        image_bytes: 原始图像字节（通常是 PNG）。
        bbox: 可选裁剪区域 (left, top, right, bottom)，依赖 Pillow，精确像素
            切片，无插值，天然无损。
        max_side: 缩放后最长边像素数（<=0 表示不缩放，默认不缩放）。依赖
            Pillow。**注意**：>0 时会触发有损插值降采样，2026-07-31 起默认
            值已由 48 改为 0（无损优先，不必考虑 rosetta_map 体积膨胀）。
        compress: 是否在 z85 编码前叠加一层 zlib 无损压缩（默认启用，严格
            可逆，不影响解码结果，仅影响编码后字符串长度）。False 时回退
            旧版无压缩层格式（"z85:" 前缀），供需要兼容旧工具链读取的
            边缘场景使用。

    Returns:
        新默认格式 "z85zl:<原始字节长度>:<z85正文>"（compress=True）或
        旧格式 "z85:<原始字节长度>:<z85正文>"（compress=False）。
    """
    processed = _maybe_crop_and_resize(image_bytes, bbox, max_side)
    if compress:
        payload = zlib.compress(processed, level=9)
        prefix = "z85zl"
    else:
        payload = processed
        prefix = "z85"
    pad_len = (-len(payload)) % 4
    padded = payload + b"\x00" * pad_len
    body = _z85_encode(padded)
    return f"{prefix}:{len(payload)}:{body}"


def decode(pattern: str) -> bytes:
    """解码 pattern 字段。三态兼容："z85zl:"（当前默认，zlib+z85）/
    "z85:"（历史，纯 z85）/ 无前缀（最旧，纯 base64）。
    """
    if pattern.startswith("z85zl:"):
        _, length_str, body = pattern.split(":", 2)
        length = int(length_str)
        padded = _z85_decode(body)
        compressed = bytes(padded[:length])
        return zlib.decompress(compressed)
    if pattern.startswith("z85:"):
        _, length_str, body = pattern.split(":", 2)
        length = int(length_str)
        padded = _z85_decode(body)
        return bytes(padded[:length])
    return base64.b64decode(pattern)


def image_similarity(original_bytes: bytes, decoded_bytes: bytes) -> float:
    """计算两张图片的像素级相似度（0.0~1.0）。

    两张图统一转 RGB 后逐像素比较，返回完全一致像素的占比。尺寸不同时
    直接判定为 0.0（本次无损默认路径下 decode() 结果尺寸必然与编码前
    一致；尺寸不同属于历史 max_side>0 缩放数据，不适用本相似度定义）。

    依赖 Pillow（项目既有可选依赖）；未安装时抛 RuntimeError，不静默返回
    错误的"相似"结论。
    """
    try:
        from PIL import Image
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "image_similarity() 依赖 Pillow + numpy，请先安装（uv pip install pillow numpy）"
        ) from exc

    img_a = Image.open(BytesIO(original_bytes)).convert("RGB")
    img_b = Image.open(BytesIO(decoded_bytes)).convert("RGB")
    if img_a.size != img_b.size:
        return 0.0

    arr_a = np.asarray(img_a)
    arr_b = np.asarray(img_b)
    total_pixels = arr_a.shape[0] * arr_a.shape[1]
    if total_pixels == 0:
        return 1.0
    matching_pixels = int(np.all(arr_a == arr_b, axis=-1).sum())
    return matching_pixels / total_pixels
