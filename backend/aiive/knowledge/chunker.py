CHUNK_SIZE = 500  # characters


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[dict]:
    lines = text.split("\n")
    chunks = []
    current = ""
    start_line = 0
    chunk_idx = 0

    for i, line in enumerate(lines):
        if current and len(current) + len(line) > chunk_size:
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

    if current.strip():
        chunks.append({
            "content": current.rstrip(),
            "line_start": start_line,
            "line_end": len(lines) - 1,
            "chunk_index": chunk_idx,
        })

    return chunks
