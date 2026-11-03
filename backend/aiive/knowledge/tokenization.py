"""确定性的 Token 计数（结构感知切分的长度口径）。

为什么不用 len(text) // 4：中文里「4 个字符 ≈ 1 个 token」的假设严重低估
（BGE 中文分词接近 1 字 1 token），会让 700 token 的上限形同虚设，切出远超模型
窗口的块。本模块给出**可重复、与平台无关的上界估算**：

* CJK（汉字 / 假名 / 谚文 / 全角标点 / CJK 标点）：1 个字符 = 1 token；
* 连续的非 CJK、非空白字符（拉丁字母、数字、半角标点、代码符号）：每 4 个
  字符 1 token，向上取整、至少 1；
* 空白不计数。

该口径对上界友好（宁可少切一点，也不超出模型窗口），且完全确定性：同一段文本
在任何机器上得到同一个数字，便于测试与幂等重建。
"""
from __future__ import annotations

TOKENIZER_VERSION = "cjk_char_latin4_v1"

# CJK 相关码位：假名、CJK 扩展 A、统一表意文字、兼容表意文字、全角形式、
# CJK 标点、谚文音节。这些字符在中文/日文 BPE 词表里通常独占或接近独占一个 token。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)


def is_cjk(char: str) -> bool:
    """字符是否属于按「1 字 1 token」计数的 CJK 区段。"""
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def _run_tokens(length: int) -> int:
    """一段连续非 CJK 字符的 token 数（每 4 字符 1 token，向上取整）。"""
    return max(1, (length + 3) // 4)


def count_tokens(text: str) -> int:
    """返回文本的确定性 token 估算值（空文本为 0）。"""
    if not text:
        return 0
    total = 0
    run = 0
    for char in text:
        if char.isspace():
            if run:
                total += _run_tokens(run)
                run = 0
        elif is_cjk(char):
            if run:
                total += _run_tokens(run)
                run = 0
            total += 1
        else:
            run += 1
    if run:
        total += _run_tokens(run)
    return total


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按同一口径截断文本，使其 token 数不超过 max_tokens（尽量按字符边界）。"""
    if max_tokens <= 0:
        return ""
    if count_tokens(text) <= max_tokens:
        return text
    total = 0
    run = 0
    for index, char in enumerate(text):
        if char.isspace():
            if run:
                total += _run_tokens(run)
                run = 0
        elif is_cjk(char):
            if run:
                total += _run_tokens(run)
                run = 0
            total += 1
        else:
            run += 1
            if total + _run_tokens(run) > max_tokens:
                return text[:index]
        if total > max_tokens:
            return text[:index]
    return text
