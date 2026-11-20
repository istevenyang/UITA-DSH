"""VisionLLMClient — 视觉 LLM 客户端抽象接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class VisionLLMClient(ABC):
    @abstractmethod
    async def locate(self, prompt: str, screenshot: bytes) -> tuple[float, float]:
        """返回目标元素中心坐标（相对于传入 screenshot 图片自身的绝对像素坐标）。"""
        ...

    @abstractmethod
    async def judge(self, question: str, screenshot: bytes) -> bool:
        """返回是非判定结果，供 L4b（assert/verify 视觉存在性判断）使用。"""
        ...

    @abstractmethod
    async def chat(self, prompt: str) -> str:
        """纯文本对话（无截图），供 L3.5 LLM_SELECTOR 使用。"""
        ...
