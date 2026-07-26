"""
文本分块模块。

将长文本按行切分为固定大小的块（chunk），用于知识库的文档分割。
采用按行累加的方式，确保每块不超过指定字符数，同时尽量保持行的完整性。
"""

from typing import Any

CHUNK_SIZE = 500  # 每个块的默认字符数


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[dict[str, Any]]:
    """
    将文本按行分割为多个块。

    参数:
        text: 待分割的原始文本。
        chunk_size: 每个块的最大字符数，默认 500。

    返回:
        块列表，每项包含 content（块内容）、line_start（起始行号）、
        line_end（结束行号）和 chunk_index（块索引）。
    """
    lines = text.split("\n")
    chunks = []
    current = ""  # 当前累积的文本
    start_line = 0  # 当前块的起始行号
    chunk_idx = 0  # 块索引计数器

    for i, line in enumerate(lines):
        # 单行超长：先冲刷当前累积块，再把该行按 chunk_size 字符级切分，
        # 避免产生任意大的块。
        if len(line) > chunk_size:
            if current.strip():
                chunks.append({
                    "content": current.rstrip(),
                    "line_start": start_line,
                    "line_end": i - 1,
                    "chunk_index": chunk_idx,
                })
                chunk_idx += 1
            current = ""
            for pos in range(0, len(line), chunk_size):
                piece = line[pos : pos + chunk_size]
                if not piece.strip():
                    continue
                chunks.append({
                    "content": piece,
                    "line_start": i,
                    "line_end": i,
                    "chunk_index": chunk_idx,
                })
                chunk_idx += 1
            start_line = i + 1
            continue

        # 如果当前块已有内容，且加入下一行（含换行符）会超出限制，
        # 则保存当前块并开始新块。
        if current and len(current) + len(line) + 1 > chunk_size:
            chunks.append({
                "content": current.rstrip(),
                "line_start": start_line,
                "line_end": i - 1,
                "chunk_index": chunk_idx,
            })
            current = line + "\n"
            start_line = i
            chunk_idx += 1
        else:
            current += line + "\n"

    # 处理最后一个块（可能不满 chunk_size）
    if current.strip():
        chunks.append({
            "content": current.rstrip(),
            "line_start": start_line,
            "line_end": len(lines) - 1,
            "chunk_index": chunk_idx,
        })

    return chunks
