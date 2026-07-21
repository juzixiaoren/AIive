"""
文本处理共享工具。

提供跨模块复用的轻量文本清洗函数，避免各处重复实现。
"""

from __future__ import annotations

import re

# 匹配开头的代码围栏（可选语言标签，如 ```json / ```），含其后换行
_LEADING_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\n?")
# 匹配结尾的代码围栏
_TRAILING_FENCE = re.compile(r"\n?```$")


def strip_code_fence(text: str) -> str:
    """剥离 LLM 输出中包裹的 markdown 代码围栏。

    处理形如 ```json\n{...}\n``` 或 ```\n...\n``` 的包裹，返回内部内容。
    未被围栏包裹时原样返回（仅去除首尾空白）。

    参数:
        text: 原始文本

    返回:
        去除首尾代码围栏后的文本
    """
    stripped = text.strip()
    stripped = _LEADING_FENCE.sub("", stripped, count=1)
    stripped = _TRAILING_FENCE.sub("", stripped, count=1)
    return stripped.strip()
