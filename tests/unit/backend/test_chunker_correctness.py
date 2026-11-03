"""V61.1 Chunker 正确性回归测试。

覆盖审计发现的四类缺陷：
1. 超长表格行 / 单元格 / 表头不得突破 max_chunk_tokens；
2. 同名标题重复出现时 section_id 不得冲突（切分与邻居扩展不跨章节）；
3. 递归拆分后的行号 / 页码定位必须准确，Overlap 不得伪造来源；
4. 无首尾竖线的 Markdown 表格必须被识别，且不误判普通段落。

另外建立统一不变量：count_tokens(chunk.content) <= max_chunk_tokens。
"""
from __future__ import annotations

import pytest

from aiive.config import settings
from aiive.db.models import Chunk, Document
from aiive.knowledge.blocks import (
    BLOCK_CODE,
    BLOCK_PARAGRAPH,
    BLOCK_TABLE,
    parse_document_blocks,
)
from aiive.knowledge.chunker import (
    ChunkConfig,
    ChunkDraft,
    _enforce_budget,
    chunk_blocks,
)
from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks
from aiive.knowledge.tokenization import count_tokens

FENCE = chr(96) * 3
MAX_TOKENS = 700


def _blocks(text: str, name: str = "doc.md"):
    return parse_document_blocks(text.encode("utf-8"), name)


def _chunks(text: str, name: str = "doc.md", config: ChunkConfig | None = None):
    return chunk_blocks(_blocks(text, name), config)


def _assert_budget(chunks, limit: int = MAX_TOKENS) -> None:
    for chunk in chunks:
        assert count_tokens(chunk.content) <= limit, (
            f"chunk {chunk.chunk_index} 超限：{count_tokens(chunk.content)} > {limit}"
        )


class TestOversizedTable:
    def test_oversized_row_is_split_and_each_part_keeps_header(self):
        cells = [f"单元格{i}标记" * 60 for i in range(3)]
        row = "| " + " | ".join(cells) + " |"
        text = "| 名称 | 值 | 说明 |\n|---|---|---|\n" + row + "\n| 普通 | 1 | 2 |\n"
        chunks = _chunks(text)

        assert len(chunks) > 1
        _assert_budget(chunks)
        for chunk in chunks:
            assert chunk.content.split("\n")[:2] == ["| 名称 | 值 | 说明 |", "|---|---|---|"]
        joined = "\n".join(chunk.content for chunk in chunks)
        # 每个单元格的标记都必须出现在某个 Chunk 里（不静默丢内容）
        for index in range(3):
            assert f"单元格{index}标记" in joined

    def test_oversized_cell_is_split_without_losing_content(self):
        cell = "超长单元格内容。" * 200
        text = "| A | B |\n|---|---|\n| " + cell + " | x |\n"
        chunks = _chunks(text)

        assert len(chunks) > 1
        _assert_budget(chunks)
        total = sum(chunk.content.count("超长单元格内容。") for chunk in chunks)
        assert total == 200

    def test_oversized_header_falls_back_without_losing_content(self):
        header = " | ".join("表头列" * 80 for _ in range(3))
        text = "| " + header + " |\n|---|---|---|\n| 1 | 2 | 3 |\n"
        chunks = _chunks(text)

        assert chunks
        _assert_budget(chunks)
        joined = "".join(chunk.content for chunk in chunks)
        assert joined.count("表头列") == 240

    def test_table_chunks_point_to_data_rows_not_header(self):
        rows = [f"| 行{i} | 值{i} | 说明{i} |" for i in range(200)]
        text = "# 表格\n\n| 名称 | 值 | 说明 |\n|---|---|---|\n" + "\n".join(rows) + "\n"
        blocks = _blocks(text)
        table = [block for block in blocks if block.block_type == BLOCK_TABLE][0]
        chunks = chunk_blocks(blocks)

        assert len(chunks) > 1
        _assert_budget(chunks)
        for chunk in chunks:
            # 表头是重复上下文，来源范围必须落在数据行内
            assert chunk.line_start >= table.line_start + 2
            assert chunk.line_end <= table.line_end
            assert chunk.context_tokens > 0


class TestDuplicateSections:
    DUPLICATE = (
        "# 手册\n"
        "\n"
        "## 概述\n"
        "\n"
        "第一段概述内容。\n"
        "\n"
        "## 概述\n"
        "\n"
        "第二段概述内容。\n"
    )

    def test_same_heading_gets_distinct_section_ids(self):
        blocks = _blocks(self.DUPLICATE)
        sections = [block for block in blocks if block.heading_path[-1:] == ("概述",)]
        ids = [block.section_id for block in sections]

        assert len(set(ids)) == 2
        # 每个标题下的正文与标题本身共享 id
        assert sections[0].section_id == sections[1].section_id
        assert sections[2].section_id == sections[3].section_id
        assert sections[1].section_id != sections[3].section_id

    def test_nested_duplicate_headings_are_distinct(self):
        text = (
            "# 根\n\n## A\n\n### B\n\n第一段。\n\n## A\n\n### B\n\n第二段。\n"
        )
        blocks = _blocks(text)
        b_blocks = [block for block in blocks if block.heading_path == ("根", "A", "B")]
        assert len({block.section_id for block in b_blocks}) == 2

    def test_document_without_headings_has_one_section(self):
        blocks = _blocks("第一段。\n\n第二段。\n\n第三段。\n")
        assert blocks
        assert len({block.section_id for block in blocks}) == 1
        assert blocks[0].section_id.startswith("s")

    def test_section_ids_are_deterministic(self):
        first = [block.section_id for block in _blocks(self.DUPLICATE)]
        second = [block.section_id for block in _blocks(self.DUPLICATE)]
        assert first == second

    def test_chunks_do_not_merge_across_duplicate_sections(self):
        text = (
            "# 手册\n\n## 概述\n\n"
            + "第一节内容。" * 60
            + "\n\n## 概述\n\n"
            + "第二节内容。" * 60
            + "\n"
        )
        chunks = _chunks(text)

        assert len(chunks) >= 2
        assert len({chunk.section_id for chunk in chunks}) == 2
        for chunk in chunks:
            assert ("第一节内容" in chunk.content) != ("第二节内容" in chunk.content)

    def test_neighbor_expansion_stays_inside_duplicate_section(self, db_session, tmp_path):
        first = "".join(f"甲{i}号说明发布流程的第{i}个细节。" for i in range(1, 121))
        second = "".join(f"乙{i}号说明回滚流程的第{i}个细节。" for i in range(1, 121))
        text = f"# 手册\n\n## 概述\n\n{first}\n\n## 概述\n\n{second}\n"
        source = tmp_path / "dup.md"
        source.write_text(text, encoding="utf-8")
        KnowledgeIngestor(db_session).ingest(str(source))
        db_session.flush()

        hits = search_chunks(db_session, "乙60号", 2)
        assert hits
        seed = hits[0]
        assert "乙60号" in seed["content_preview"]
        for hit in hits:
            assert hit["section_id"] == seed["section_id"]
            assert "甲" not in hit["content_preview"]


class TestSourceLocations:
    def test_oversized_paragraph_chunks_have_increasing_line_ranges(self):
        paragraphs = [f"第{index}段：" + "内容说明。" * 40 for index in range(1, 6)]
        text = "# 标题\n\n" + "\n\n".join(paragraphs) + "\n"
        blocks = _blocks(text)
        paragraphs = [item for item in blocks if item.block_type == BLOCK_PARAGRAPH]
        chunks = _chunks(text)

        assert len(chunks) > 1
        ranges = [(chunk.line_start, chunk.line_end) for chunk in chunks]
        assert ranges == sorted(ranges)
        assert len({item[0] for item in ranges}) == len(ranges)
        lower = min(item.line_start for item in paragraphs)
        upper = max(item.line_end for item in paragraphs)
        for start, end in ranges:
            assert lower <= start <= end <= upper
        # 每个 Chunk 的行号必须指向真实存在的段落起始行
        starts = {item.line_start for item in paragraphs}
        assert {start for start, _ in ranges} <= starts

    def test_oversized_code_chunks_track_function_lines(self):
        functions = []
        for index in range(6):
            functions.append(
                f"def handler_{index}(value):\n    return value + {index}\n"
                + "    # " + "注释" * 60
            )
        code = "\n\n".join(functions)
        text = "# 代码\n\n" + FENCE + "python\n" + code + "\n" + FENCE + "\n"
        blocks = _blocks(text, "code.md")
        code_block = [item for item in blocks if item.block_type == BLOCK_CODE][0]
        source_lines = code_block.content.split("\n")
        chunks = _chunks(text, "code.md")

        assert len(chunks) > 1
        for chunk in chunks:
            first_line = chunk.content.split("\n")[0]
            expected = code_block.line_start + source_lines.index(first_line)
            assert chunk.line_start == expected
            assert chunk.line_end >= chunk.line_start

    def test_overlap_prefix_does_not_claim_source_range(self):
        text = "# 标题\n\n" + "\n\n".join(
            f"第{index}段：" + "内容说明。" * 40 for index in range(1, 6)
        ) + "\n"
        source_lines = text.split("\n")
        chunks = _chunks(text)
        overlapped = [chunk for chunk in chunks if chunk.context_tokens > 0]

        assert overlapped
        for chunk in overlapped:
            line = source_lines[chunk.line_start]
            assert line.startswith("第")
            assert line in chunk.content
            # Overlap 文本来自上一块，不能把 line_start 拉回上一块的范围
            assert chunk.content.index(line) > 0

    def test_pdf_split_keeps_page_number(self):
        from tests.unit.backend.test_structure_chunking import _build_pdf

        paragraph = "PDF page content line for chunking. " * 200
        data = _build_pdf([paragraph])
        blocks = parse_document_blocks(data, "sample.pdf")
        chunks = chunk_blocks(blocks)

        assert len(chunks) > 1
        _assert_budget(chunks)
        assert all(chunk.page_start == 1 and chunk.page_end == 1 for chunk in chunks)


class TestPipeLessTables:
    def test_table_without_outer_pipes_is_recognized(self):
        text = "名称 | 年龄\n--- | ---\n张三 | 20\n李四 | 25\n"
        blocks = _blocks(text)
        assert [block.block_type for block in blocks] == [BLOCK_TABLE]
        assert "张三" in blocks[0].content

    def test_columnar_table_is_recognized(self):
        text = "名称 年龄\n张三 20\n李四 25\n"
        blocks = _blocks(text)
        assert [block.block_type for block in blocks] == [BLOCK_TABLE]
        chunks = chunk_blocks(blocks)
        assert chunks and chunks[0].block_type == BLOCK_TABLE

    def test_pipe_table_still_works(self):
        text = "| 名称 | 年龄 |\n|---|---|\n| 张三 | 20 |\n"
        blocks = _blocks(text)
        assert [block.block_type for block in blocks] == [BLOCK_TABLE]

    def test_plain_prose_is_not_mistaken_for_table(self):
        text = "今天 天气 很好\n明天 天气 一般\n后天 天气 转晴\n"
        blocks = _blocks(text)
        assert [block.block_type for block in blocks] == [BLOCK_PARAGRAPH]

    def test_prose_with_punctuation_is_not_a_table(self):
        text = "第一行，说明。\n第二行，说明。\n第三行，说明。\n"
        blocks = _blocks(text)
        assert all(block.block_type == BLOCK_PARAGRAPH for block in blocks)


class TestBudgetInvariant:
    MIXED = (
        "# 指南\n\n"
        "## 段落\n\n"
        + "段落内容说明。" * 120
        + "\n\n- 列表项一\n- 列表项二\n  - 子项\n\n"
        "> 引用内容说明。" * 30
        + "\n\n## 代码\n\n"
        + FENCE + "python\n"
        + "\n\n".join(f"def f{i}(x):\n    return x + {i}" for i in range(30))
        + "\n" + FENCE
        + "\n\n## 表格\n\n"
        + "\n".join(f"| 行{i} | 值{i} | 说明{i} |" for i in range(150))
        + "\n"
    )

    def test_all_chunks_respect_small_budget(self):
        config = ChunkConfig(
            target_chunk_tokens=120, max_chunk_tokens=200,
            min_chunk_tokens=40, overlap_tokens=30,
        )
        chunks = _chunks(self.MIXED, config=config)

        assert chunks
        _assert_budget(chunks, limit=200)
        assert any(chunk.context_tokens > 0 for chunk in chunks)

    def test_all_chunks_respect_default_budget(self):
        chunks = _chunks(self.MIXED)
        _assert_budget(chunks)

    def test_enforce_budget_splits_oversized_draft(self):
        draft = ChunkDraft(
            content="内容说明。" * 800,
            heading_path=("标题",),
            block_type=BLOCK_PARAGRAPH,
            section_id="s-test",
            line_start=10,
            line_end=20,
            page_start=None,
            page_end=None,
            chunk_index=0,
            token_count=0,
        )
        config = ChunkConfig()
        parts = _enforce_budget([draft], config)

        assert len(parts) > 1
        _assert_budget(parts)
        assert sum(part.content.count("内容说明。") for part in parts) == 800
        assert all(part.section_id == "s-test" for part in parts)
        assert all(10 <= part.line_start <= part.line_end <= 20 for part in parts)
