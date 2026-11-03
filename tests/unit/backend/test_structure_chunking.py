"""结构感知解析与递归切分的单元测试（离线，无外部服务）。

覆盖：
- 确定性 Token 计数（CJK 1 字 1 token，禁止 len//4）；
- Markdown / DOCX / PDF 的结构化 Block 解析（标题路径、块类型、页码、代码缩进）；
- 递归切分：Section 边界、超长段落、代码块、表格表头复制、短尾块、Overlap；
- Chunk 结构元数据与 Embedding 输入（标题上下文、哈希、模板版本）；
- 版本变化（chunker / 模板 / 模型）触发重建，以及检索命中的结构定位与邻居扩展。
"""
from __future__ import annotations

import zipfile

from aiive.config import settings
from aiive.db.models import Chunk, Document, KnowledgeChunkVector
from aiive.knowledge import blocks as blocks_module
from aiive.knowledge import chunk_vectors
from aiive.knowledge.blocks import (
    Block,
    BLOCK_CODE,
    BLOCK_HEADING,
    BLOCK_LIST_ITEM,
    BLOCK_PARAGRAPH,
    BLOCK_QUOTE,
    BLOCK_TABLE,
    BLOCK_TEXT,
    PARSER_VERSION,
    heading_path_text,
    heading_path_tuple,
    parse_document_blocks,
)
from aiive.knowledge.chunk_vectors import ChunkVectorService, missing_vector_document_ids
from aiive.knowledge.chunker import CHUNKER_VERSION, ChunkConfig, chunk_blocks
from aiive.knowledge.embedding_input import (
    EMBEDDING_INPUT_VERSION,
    build_embedding_input,
    embedding_input_hash,
)
from aiive.knowledge.ingestor import KnowledgeIngestor, search_chunks
from aiive.knowledge.tokenization import TOKENIZER_VERSION, count_tokens, truncate_to_tokens

FENCE = chr(96) * 3

MARKDOWN = (
    "# Redis 技术笔记\n"
    "\n"
    "Redis 是一个内存数据库。\n"
    "\n"
    "## 持久化\n"
    "\n"
    "通过记录写命令实现持久化。\n"
    "\n"
    "### AOF\n"
    "\n"
    "AOF 追加写命令到文件。\n"
    "\n"
    "- 先写缓冲区\n"
    "- 再刷盘\n"
    "  - 子项说明\n"
    "- 最后重写\n"
    "\n"
    "> 引用：官方文档建议每秒刷盘。\n"
    "\n"
    "| 模式 | 优点 | 缺点 |\n"
    "|---|---|---|\n"
    "| RDB | 文件小 | 可能丢数据 |\n"
    "| AOF | 丢失少 | 文件大 |\n"
    "\n"
    + FENCE + "python\n"
    "def append(command):\n"
    "    return command\n"
    + FENCE + "\n"
)


def _write_docx(path, body_xml: str) -> None:
    """写一个只含 word/document.xml 的最小 DOCX。"""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_xml}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def _docx_paragraph(text: str, style: str | None = None) -> str:
    pr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{pr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _build_pdf(pages: list[str]) -> bytes:
    """构造一个每页一段文本的最小 PDF（用于页码定位测试）。"""
    objects: dict[int, bytes] = {}
    page_ids: list[int] = []
    font_id = 3
    next_id = 4
    for text in pages:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        page_ids.append(page_id)
        stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
            f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        ).encode("latin-1")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
        )
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for oid in sorted(objects):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode() + objects[oid] + b"\nendobj\n"
    xref_at = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for oid in range(1, size):
        out += f"{offsets[oid]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def _long_paragraph(sentences: int = 120) -> str:
    return "".join(f"第{i}句话说明持久化的第{i}个细节。" for i in range(1, sentences + 1))


class _FakeEmbedder:
    """记录每次 Embedding 输入，返回固定维度向量。"""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def embed_batch(
        self, texts: list[str], *, input_type: str = "document", batch_size: int = 8,
    ) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[0.01] * 512 for _ in texts]

    def embed(self, text: str, *, input_type: str = "query") -> list[float]:
        return [0.01] * 512


class TestTokenization:
    def test_counts_cjk_per_character_not_len_over_four(self):
        assert count_tokens("") == 0
        assert count_tokens("   ") == 0
        assert count_tokens("中文测试") == 4
        assert count_tokens("中" * 400) == 400

    def test_counts_latin_runs_by_four_characters(self):
        assert count_tokens("hello world") == 4
        assert count_tokens("Redis 持久化") == 5
        assert TOKENIZER_VERSION == "cjk_char_latin4_v1"

    def test_truncate_respects_budget(self):
        text = "中" * 100
        truncated = truncate_to_tokens(text, 10)
        assert truncated == "中" * 10
        assert count_tokens(truncated) <= 10
        assert truncate_to_tokens("短文本", 10) == "短文本"
        assert truncate_to_tokens("短文本", 0) == ""


class TestBlockParsing:
    def test_markdown_blocks_cover_headings_paragraphs_lists_quote_table_code(self):
        blocks = parse_document_blocks(MARKDOWN.encode("utf-8"), "notes.md")
        types = [block.block_type for block in blocks]

        assert types[0] == BLOCK_HEADING and blocks[0].content == "Redis 技术笔记"
        assert BLOCK_PARAGRAPH in types and BLOCK_LIST_ITEM in types
        assert BLOCK_QUOTE in types and BLOCK_TABLE in types and BLOCK_CODE in types
        # 标题不重复进入正文块，列表项保留缩进的子项
        assert [block.content for block in blocks if block.block_type == BLOCK_LIST_ITEM] == [
            "- 先写缓冲区", "- 再刷盘", "  - 子项说明", "- 最后重写",
        ]

    def test_heading_path_and_section_id_follow_document_hierarchy(self):
        blocks = parse_document_blocks(MARKDOWN.encode("utf-8"), "notes.md")
        aof = [block for block in blocks if block.heading_path[-1] == "AOF"]
        top = [block for block in blocks if block.heading_path == ("Redis 技术笔记",)]

        assert all(block.heading_path == ("Redis 技术笔记", "持久化", "AOF") for block in aof)
        assert heading_path_text(aof[0].heading_path) == "Redis 技术笔记 > 持久化 > AOF"
        assert heading_path_tuple(heading_path_text(aof[0].heading_path)) == aof[0].heading_path
        # 不同 Section 的 section_id 不同，同 Section 内一致
        assert len({block.section_id for block in aof}) == 1
        assert aof[0].section_id != top[1].section_id
        assert all(block.section_id.startswith("s") for block in blocks)

    def test_section_id_is_stable_across_reparse(self):
        first = parse_document_blocks(MARKDOWN.encode("utf-8"), "notes.md")
        second = parse_document_blocks(MARKDOWN.encode("utf-8"), "notes.md")
        assert [block.section_id for block in first] == [block.section_id for block in second]

    def test_code_block_preserves_indentation_and_drops_fences(self):
        blocks = parse_document_blocks(MARKDOWN.encode("utf-8"), "notes.md")
        code = [block for block in blocks if block.block_type == BLOCK_CODE]
        assert len(code) == 1
        assert code[0].content == "def append(command):\n    return command"

    def test_docx_uses_heading_styles_and_tables(self, tmp_path):
        body = (
            _docx_paragraph("季度报告", "Heading1")
            + _docx_paragraph("收入增长 20%")
            + _docx_paragraph("持久化", "Heading2")
            + "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>模式</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>优点</w:t></w:r></w:p></w:tc></w:tr>"
            "<w:tr><w:tc><w:p><w:r><w:t>RDB</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>文件小</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        )
        source = tmp_path / "report.docx"
        _write_docx(source, body)

        blocks = parse_document_blocks(source.read_bytes(), "report.docx")
        assert [block.block_type for block in blocks] == [
            BLOCK_HEADING, BLOCK_PARAGRAPH, BLOCK_HEADING, BLOCK_TABLE,
        ]
        assert blocks[0].heading_path == ("季度报告",)
        assert blocks[1].heading_path == ("季度报告",)
        assert blocks[3].heading_path == ("季度报告", "持久化")
        assert "RDB" in blocks[3].content

    def test_pdf_blocks_carry_page_numbers_without_fake_headings(self):
        data = _build_pdf(["Page one text", "Page two text"])
        blocks = parse_document_blocks(data, "sample.pdf")

        assert [(block.page_start, block.page_end) for block in blocks] == [(1, 1), (2, 2)]
        assert all(block.block_type == BLOCK_PARAGRAPH for block in blocks)
        assert all(block.heading_path == () for block in blocks)

    def test_parser_failure_falls_back_to_generic_text_blocks(self, monkeypatch):
        def boom(text: str):
            raise RuntimeError("markdown parser exploded")

        monkeypatch.setattr(blocks_module, "_markdown_blocks", boom)
        blocks = parse_document_blocks(MARKDOWN.encode("utf-8"), "notes.md")
        joined = "".join(block.content for block in blocks)

        # 解析失败不静默丢内容：正文全部退回通用文本 Block
        assert "Redis 技术笔记" in joined
        assert "通过记录写命令实现持久化。" in joined
        assert all(block.block_type == BLOCK_TEXT for block in blocks)


class TestRecursiveChunking:
    def test_chunks_skip_headings_and_keep_structure(self):
        blocks = parse_document_blocks(MARKDOWN.encode("utf-8"), "notes.md")
        chunks = chunk_blocks(blocks)

        assert chunks
        assert all(chunk.heading_path for chunk in chunks)
        assert all(not chunk.content.lstrip().startswith("#") for chunk in chunks)
        assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
        # 同一 Section 的块落在同一个 Chunk 里，AOF 下的段落/列表/引用保持连续
        aof = [chunk for chunk in chunks if chunk.heading_path[-1] == "AOF"]
        assert any("先写缓冲区" in chunk.content for chunk in aof)
        assert any(chunk.block_type == BLOCK_TABLE for chunk in aof)
        assert any(chunk.block_type == BLOCK_CODE for chunk in aof)

    def test_oversized_paragraph_is_split_within_max_and_overlaps(self):
        text = "# 主题\n\n## 第一节\n\n" + _long_paragraph() + "\n"
        chunks = chunk_blocks(parse_document_blocks(text.encode("utf-8"), "long.md"))

        assert len(chunks) > 1
        assert all(count_tokens(chunk.content) <= 700 for chunk in chunks)
        # 相邻 Chunk 在句子边界上重叠：后一块的首句出现在前一块里
        for previous, current in zip(chunks, chunks[1:]):
            first_sentence = current.content.split("\n\n")[0]
            assert first_sentence and first_sentence in previous.content

    def test_overlap_never_crosses_sections(self):
        text = (
            "# 主题\n\n## 第一节\n\n" + _long_paragraph() + "\n\n## 第二节\n\n短内容。\n"
        )
        chunks = chunk_blocks(parse_document_blocks(text.encode("utf-8"), "two.md"))
        second = [chunk for chunk in chunks if chunk.heading_path[-1] == "第二节"]

        assert len(second) == 1
        assert second[0].content == "短内容。"

    def test_code_block_stays_atomic_and_is_not_overlapped(self):
        text = (
            "# 指南\n\n## 代码\n\n"
            + FENCE + "python\n"
            "def append(command):\n"
            "    return command\n"
            + FENCE + "\n\n"
            + "## 说明\n\n"
            + _long_paragraph(40)
            + "\n"
        )
        chunks = chunk_blocks(parse_document_blocks(text.encode("utf-8"), "code.md"))
        code_chunks = [chunk for chunk in chunks if chunk.block_type == BLOCK_CODE]
        text_chunks = [chunk for chunk in chunks if chunk.heading_path[-1] == "说明"]

        assert len(code_chunks) == 1
        assert code_chunks[0].content == "def append(command):\n    return command"
        assert all("return command" not in chunk.content for chunk in text_chunks)

    def test_oversized_code_splits_on_function_boundaries(self):
        functions = "\n\n".join(
            f"def handler_{index}(value):\n    return value + {index}\n    # "
            + "填充说明" * 40
            for index in range(12)
        )
        text = "# 代码\n\n" + FENCE + "python\n" + functions + "\n" + FENCE + "\n"
        chunks = chunk_blocks(parse_document_blocks(text.encode("utf-8"), "big.md"))

        assert len(chunks) > 1
        assert all(count_tokens(chunk.content) <= 700 for chunk in chunks)
        assert all(chunk.block_type == BLOCK_CODE for chunk in chunks)
        # 每个 Chunk 都从顶层 def 开始，说明按函数边界而非任意字符切分
        assert all(chunk.content.startswith("def handler_") for chunk in chunks)

    def test_oversized_table_repeats_header_rows(self):
        rows = "\n".join(f"| 行{i} | 值{i} | 说明{i} |" for i in range(120))
        text = "# 表格\n\n| 名称 | 值 | 说明 |\n|---|---|---|\n" + rows + "\n"
        chunks = chunk_blocks(parse_document_blocks(text.encode("utf-8"), "table.md"))

        assert len(chunks) > 1
        for chunk in chunks:
            head = chunk.content.split("\n")[:2]
            assert head == ["| 名称 | 值 | 说明 |", "|---|---|---|"]
            assert count_tokens(chunk.content) <= 700

    def test_short_tail_is_merged_into_previous_chunk(self):
        config = ChunkConfig(
            target_chunk_tokens=200, max_chunk_tokens=300,
            min_chunk_tokens=100, overlap_tokens=0,
        )
        text = "# 主题\n\n## 小节\n\n" + "第一段内容。" * 80 + "\n\n尾。\n"
        chunks = chunk_blocks(parse_document_blocks(text.encode("utf-8"), "tail.md"), config)

        assert len(chunks) == 1 or chunks[-1].content.strip() != "尾。"
        assert all(count_tokens(chunk.content) <= 300 for chunk in chunks)

    def test_empty_blocks_produce_no_chunks(self):
        assert chunk_blocks([]) == []
        blank = Block(
            content="   ", block_type=BLOCK_PARAGRAPH, heading_path=(),
            block_index=0, line_start=0, line_end=0,
        )
        assert chunk_blocks([blank]) == []

    def test_chunk_config_rejects_invalid_budgets(self):
        for kwargs in (
            {"min_chunk_tokens": 0},
            {"target_chunk_tokens": 10, "min_chunk_tokens": 100},
            {"max_chunk_tokens": 100, "target_chunk_tokens": 450},
            {"overlap_tokens": 700},
        ):
            try:
                ChunkConfig(**kwargs)
            except ValueError:
                continue
            raise AssertionError(f"非法预算未被拒绝：{kwargs}")


class TestEmbeddingInput:
    def test_builds_title_and_heading_context(self):
        text = build_embedding_input(
            title="Redis 技术笔记",
            heading_path=("Redis", "持久化", "AOF"),
            content="通过记录写命令实现持久化。",
        )
        assert text == (
            "文档：Redis 技术笔记\n章节：Redis > 持久化 > AOF\n正文：通过记录写命令实现持久化。"
        )
        assert EMBEDDING_INPUT_VERSION == "title_context_v1"

    def test_omits_missing_title_or_heading(self):
        assert build_embedding_input(title=None, heading_path=(), content="正文") == "正文：正文"
        assert build_embedding_input(
            title="  ", heading_path=(), content="正文",
        ) == "正文：正文"

    def test_hash_covers_title_heading_and_content(self):
        base = dict(heading_path=("A",), content="正文")
        assert embedding_input_hash(
            build_embedding_input(title="文档", **base)
        ) != embedding_input_hash(build_embedding_input(title="改名", **base))
        assert embedding_input_hash(
            build_embedding_input(title="文档", heading_path=("A",), content="正文")
        ) != embedding_input_hash(
            build_embedding_input(title="文档", heading_path=("A", "B"), content="正文")
        )
        assert embedding_input_hash(
            build_embedding_input(title="文档", **base)
        ) == embedding_input_hash(build_embedding_input(title="文档", **base))


class TestIngestStructureMetadata:
    def test_ingest_persists_structure_metadata_and_embedding_hash(self, db_session, tmp_path):
        source = tmp_path / "notes.md"
        source.write_text(MARKDOWN, encoding="utf-8")

        result = KnowledgeIngestor(db_session).ingest(str(source))
        db_session.flush()
        document = db_session.get(Document, result["document_id"])
        chunks = (
            db_session.query(Chunk)
            .filter(Chunk.document_id == result["document_id"])
            .order_by(Chunk.chunk_index.asc())
            .all()
        )

        assert document is not None
        assert document.parser_version == PARSER_VERSION
        assert document.chunker_version == CHUNKER_VERSION
        assert chunks
        for chunk in chunks:
            assert chunk.parser_version == PARSER_VERSION
            assert chunk.chunker_version == CHUNKER_VERSION
            assert chunk.source_type == document.doc_type
            assert chunk.section_id.startswith("s")
            assert chunk.token_estimate == count_tokens(chunk.content)
            expected = build_embedding_input(
                title=document.title,
                heading_path=heading_path_tuple(chunk.heading_path),
                content=chunk.content,
            )
            assert chunk.embedding_input_hash == embedding_input_hash(expected)

    def test_chunker_version_change_rebuilds_unchanged_document(
        self, db_session, tmp_path, monkeypatch,
    ):
        source = tmp_path / "notes.md"
        source.write_text(MARKDOWN, encoding="utf-8")
        ingestor = KnowledgeIngestor(db_session)
        first = ingestor.ingest(str(source))
        db_session.flush()
        before = {
            chunk.id
            for chunk in db_session.query(Chunk).filter(Chunk.document_id == first["document_id"])
        }

        monkeypatch.setattr("aiive.knowledge.ingestor.CHUNKER_VERSION", "structure_recursive_v2")
        second = ingestor.ingest(str(source))
        db_session.flush()
        document = db_session.get(Document, first["document_id"])
        after = {
            chunk.id
            for chunk in db_session.query(Chunk).filter(Chunk.document_id == first["document_id"])
        }

        assert second["duplicate"] is True
        assert document is not None and document.chunker_version == "structure_recursive_v2"
        assert before and after and before.isdisjoint(after)

    def test_reindex_rebuilds_structure_from_persisted_source(self, db_session, tmp_path):
        source = tmp_path / "notes.md"
        source.write_text(MARKDOWN, encoding="utf-8")
        ingestor = KnowledgeIngestor(db_session)
        result = ingestor.ingest(str(source))
        db_session.query(Chunk).filter(Chunk.document_id == result["document_id"]).delete()
        db_session.flush()

        ingestor.reindex(result["document_id"])
        db_session.flush()
        chunks = db_session.query(Chunk).filter(Chunk.document_id == result["document_id"]).all()

        assert chunks
        assert all(chunk.heading_path for chunk in chunks)
        assert all(chunk.chunker_version == CHUNKER_VERSION for chunk in chunks)


    def test_content_update_supersedes_old_version_and_keeps_structure(self, db_session, tmp_path):
        source = tmp_path / "notes.md"
        source.write_text("# 旧标题\n\n旧内容说明。\n", encoding="utf-8")
        ingestor = KnowledgeIngestor(db_session)
        first = ingestor.ingest(str(source))
        db_session.flush()

        source.write_text("# 新标题\n\n新内容说明更详细。\n", encoding="utf-8")
        second = ingestor.ingest(str(source))
        db_session.flush()

        old = db_session.get(Document, first["document_id"])
        new = db_session.get(Document, second["document_id"])
        assert old is not None and new is not None
        assert new.id != old.id
        assert old.status == "superseded"
        assert old.superseded_by == new.id

        chunks = db_session.query(Chunk).filter(Chunk.document_id == new.id).all()
        assert [chunk.heading_path for chunk in chunks] == ["新标题"]
        assert "旧内容" not in "".join(chunk.content for chunk in chunks)
        # 旧版本即使分块尚未清理，也不会被检索命中（只检索活跃文档）
        hits = search_chunks(db_session, "旧内容说明", 5)
        assert all(hit["document_id"] != old.id for hit in hits)


class TestVectorFreshness:
    def _ingest(self, db_session, tmp_path):
        source = tmp_path / "notes.md"
        source.write_text(MARKDOWN, encoding="utf-8")
        result = KnowledgeIngestor(db_session).ingest(str(source))
        db_session.flush()
        return result["document_id"]

    def _rows(self, db_session, document_id):
        return (
            db_session.query(KnowledgeChunkVector)
            .join(Chunk, Chunk.id == KnowledgeChunkVector.chunk_id)
            .filter(Chunk.document_id == document_id)
            .all()
        )

    def test_refresh_embeds_title_context_and_is_idempotent(self, db_session, tmp_path):
        document_id = self._ingest(db_session, tmp_path)
        fake = _FakeEmbedder()
        service = ChunkVectorService(db_session, provider_factory=lambda: fake)

        service.refresh_document(document_id)
        db_session.flush()
        assert len(fake.batches) == 1
        assert all(text.startswith("文档：notes.md\n章节：") for text in fake.batches[0])

        rows = self._rows(db_session, document_id)
        assert rows and all(row.input_version == EMBEDDING_INPUT_VERSION for row in rows)
        assert all(row.embedding_model == chunk_vectors.embedding_model_identity() for row in rows)

        service.refresh_document(document_id)
        db_session.flush()
        assert len(fake.batches) == 1

    def test_title_change_invalidates_projection(self, db_session, tmp_path):
        document_id = self._ingest(db_session, tmp_path)
        fake = _FakeEmbedder()
        service = ChunkVectorService(db_session, provider_factory=lambda: fake)
        service.refresh_document(document_id)
        db_session.flush()

        document = db_session.get(Document, document_id)
        document.title = "改名后的笔记"
        db_session.flush()
        service.refresh_document(document_id)
        db_session.flush()

        assert len(fake.batches) == 2
        assert all(text.startswith("文档：改名后的笔记") for text in fake.batches[1])

    def test_template_version_change_invalidates_projection(
        self, db_session, tmp_path, monkeypatch,
    ):
        document_id = self._ingest(db_session, tmp_path)
        fake = _FakeEmbedder()
        service = ChunkVectorService(db_session, provider_factory=lambda: fake)
        service.refresh_document(document_id)
        db_session.flush()

        monkeypatch.setattr(chunk_vectors, "EMBEDDING_INPUT_VERSION", "title_context_v2")
        service.refresh_document(document_id)
        db_session.flush()

        rows = self._rows(db_session, document_id)
        assert len(fake.batches) == 2
        assert all(row.input_version == "title_context_v2" for row in rows)

    def test_model_change_invalidates_projection(self, db_session, tmp_path, monkeypatch):
        document_id = self._ingest(db_session, tmp_path)
        fake = _FakeEmbedder()
        service = ChunkVectorService(db_session, provider_factory=lambda: fake)
        service.refresh_document(document_id)
        db_session.flush()

        monkeypatch.setattr(settings, "aiive_embedding_model", "another-embedding-model")
        service.refresh_document(document_id)
        db_session.flush()

        identity = chunk_vectors.embedding_model_identity()
        rows = self._rows(db_session, document_id)
        assert len(fake.batches) == 2
        assert all(row.embedding_model == identity for row in rows)

    def test_missing_vector_documents_include_stale_template_version(
        self, db_session, tmp_path, monkeypatch,
    ):
        document_id = self._ingest(db_session, tmp_path)
        fake = _FakeEmbedder()
        ChunkVectorService(db_session, provider_factory=lambda: fake).refresh_document(document_id)
        db_session.flush()
        assert missing_vector_document_ids(db_session, 10) == []

        monkeypatch.setattr(chunk_vectors, "EMBEDDING_INPUT_VERSION", "title_context_v2")
        assert missing_vector_document_ids(db_session, 10) == [document_id]


class TestSearchContext:
    def test_hits_expose_structure_and_expand_same_section_neighbors(
        self, db_session, tmp_path,
    ):
        text = "# 主题\n\n## 第一节\n\n" + _long_paragraph() + "\n\n## 第二节\n\n另一主题内容。\n"
        source = tmp_path / "long.md"
        source.write_text(text, encoding="utf-8")
        result = KnowledgeIngestor(db_session).ingest(str(source))
        db_session.flush()

        hits = search_chunks(db_session, "第60句话", 2)
        assert hits
        seed = hits[0]
        assert seed["document_id"] == result["document_id"]
        assert seed["heading_path"] == "主题 > 第一节"
        assert seed["section_id"].startswith("s")
        assert seed["block_type"] in {"paragraph", "mixed"}
        assert seed["page_start"] is None

        neighbors = [hit for hit in hits[1:] if hit.get("expanded_from")]
        assert neighbors, "同 Section 的相邻分块应作为上下文返回"
        assert all(hit["section_id"] == seed["section_id"] for hit in neighbors)
        assert all(hit["heading_path"] == seed["heading_path"] for hit in neighbors)
        assert len({hit["chunk_id"] for hit in hits}) == len(hits)

    def test_neighbor_expansion_can_be_disabled(self, db_session, tmp_path, monkeypatch):
        text = "# 主题\n\n## 第一节\n\n" + _long_paragraph() + "\n"
        source = tmp_path / "long.md"
        source.write_text(text, encoding="utf-8")
        KnowledgeIngestor(db_session).ingest(str(source))
        db_session.flush()

        monkeypatch.setattr(settings, "aiive_knowledge_neighbor_expansion", 0)
        hits = search_chunks(db_session, "第60句话", 2)
        assert hits and all("expanded_from" not in hit for hit in hits)
