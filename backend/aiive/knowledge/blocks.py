"""结构感知的文档解析：把各种格式拆成带层级与位置信息的 Block。

统一契约
--------
每个 :class:`Block` 至少携带 content / block_type / heading_path / 来源位置
（line_start、line_end、page_start、page_end）/ block_index / section_id。
下游切分器只依赖这份契约，不再关心文档格式。

格式覆盖
--------
* Markdown：H1-H6、段落、列表、表格、引用、围栏代码块；
* DOCX：Heading 样式（含中文「标题 N」与 outlineLvl）、段落、表格；
* PDF：按页提取，页码写入 page_start/page_end，**不伪造标题层级**（PDF 没有可靠
  的标题结构，猜出来的层级会污染 Embedding 前缀）；
* HTML：h1-h6、p、li、pre、table；
* CSV/TSV：首行作表头，整表作为一个 table Block（切分时复制表头）；
* JSON：保留缩进结构，作为 code Block；
* 源码：按顶层 def/class/function 等边界拆成 code Block，识别不到边界时整体作为一个
  Block，由切分器按行兜底；
* 其它文本：按空行分段。

兜底原则
--------
任何格式的专用解析抛异常时，退回 :func:`_generic_blocks`（按空行分段、逐行保留），
保证原文内容不会静默丢失；解析器版本记录在 PARSER_VERSION 中，版本变化会触发重建。
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar, override
from xml.etree import ElementTree

from aiive.knowledge.document_reader import (
    _decode_text,  # pyright: ignore[reportPrivateUsage]
    extract_document_bytes,
)

logger = logging.getLogger(__name__)

PARSER_VERSION = "structure_v1"

# Block 类型：下游切分器按类型决定是否允许 Overlap、如何拆分。
BLOCK_HEADING = "heading"
BLOCK_PARAGRAPH = "paragraph"
BLOCK_LIST_ITEM = "list_item"
BLOCK_TABLE = "table"
BLOCK_CODE = "code"
BLOCK_QUOTE = "quote"
BLOCK_TEXT = "text"

BLOCK_TYPES: frozenset[str] = frozenset({
    BLOCK_HEADING, BLOCK_PARAGRAPH, BLOCK_LIST_ITEM, BLOCK_TABLE, BLOCK_CODE,
    BLOCK_QUOTE, BLOCK_TEXT,
})

# 原子块：代码与表格不参与普通文本 Overlap，也不与段落合并。
ATOMIC_BLOCK_TYPES: frozenset[str] = frozenset({BLOCK_CODE, BLOCK_TABLE})

CODE_SUFFIXES: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
})

_HEADING_PATH_SEPARATOR = " > "


@dataclass(frozen=True)
class Block:
    """文档中的一个结构单元（标题 / 段落 / 列表项 / 表格 / 代码 / 引用）。"""

    content: str
    block_type: str
    heading_path: tuple[str, ...]
    block_index: int
    line_start: int
    line_end: int
    page_start: int | None = None
    page_end: int | None = None
    section_id: str = ""

    @property
    def is_atomic(self) -> bool:
        """代码块与表格是原子单元：不参与普通文本 Overlap，不与段落合并。"""
        return self.block_type in ATOMIC_BLOCK_TYPES


@dataclass
class _Raw:
    """解析中间态：尚未套用标题层级与全局序号。"""

    content: str
    block_type: str
    line_start: int
    line_end: int
    level: int = 0
    page_start: int | None = None
    page_end: int | None = None


def _decode_preserving_indent(data: bytes) -> str:
    """解码文本并保留行首缩进。

    document_reader._clean_text 会把每一行开头的空格折叠掉（对纯展示文本没问题），
    但代码缩进与 Markdown 嵌套列表是结构信息，必须在解析前保留。
    """
    text = _decode_text(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text


def heading_path_text(heading_path: tuple[str, ...]) -> str:
    """把标题路径渲染为展示/Embedding 用的 "A > B > C"。"""
    return _HEADING_PATH_SEPARATOR.join(heading_path)


def heading_path_tuple(text: str | None) -> tuple[str, ...]:
    """把持久化的标题路径还原为元组（重建 Embedding 输入时使用）。"""
    if not text:
        return ()
    return tuple(part for part in text.split(_HEADING_PATH_SEPARATOR) if part)


def section_id_for(heading_path: tuple[str, ...]) -> str:
    """由标题路径推导稳定的 section 标识（同一路径在重建后保持不变）。"""
    digest = hashlib.sha1(
        heading_path_text(heading_path).encode("utf-8")
    ).hexdigest()
    return f"s{digest[:16]}"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_QUOTE_LINE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _markdown_blocks(text: str) -> list[_Raw]:
    """按 Markdown 语法拆 Block：标题 / 段落 / 列表 / 表格 / 引用 / 代码块。"""
    lines = text.split("\n")
    raws: list[_Raw] = []
    index = 0
    paragraph: list[str] = []
    paragraph_start = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            raws.append(_Raw(
                content="\n".join(paragraph).strip(),
                block_type=BLOCK_PARAGRAPH,
                line_start=paragraph_start,
                line_end=paragraph_start + len(paragraph) - 1,
            ))
            paragraph = []

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if _FENCE.match(line):
            flush_paragraph()
            fence = _FENCE.match(line)
            assert fence is not None
            marker = fence.group(1)
            start = index
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(marker):
                body.append(lines[index])
                index += 1
            end = min(index, len(lines) - 1)
            if body:
                raws.append(_Raw(
                    content="\n".join(body).rstrip(),
                    block_type=BLOCK_CODE,
                    line_start=start + 1,
                    line_end=end - 1 if index < len(lines) else end,
                ))
            index += 1
            continue

        heading = _ATX_HEADING.match(line)
        if heading:
            flush_paragraph()
            raws.append(_Raw(
                content=heading.group(2).strip(),
                block_type=BLOCK_HEADING,
                line_start=index,
                line_end=index,
                level=len(heading.group(1)),
            ))
            index += 1
            continue

        if stripped.startswith("|") and index + 1 < len(lines) and _TABLE_SEPARATOR.match(lines[index + 1]):
            flush_paragraph()
            start = index
            rows: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(lines[index].strip())
                index += 1
            raws.append(_Raw(
                content="\n".join(rows),
                block_type=BLOCK_TABLE,
                line_start=start,
                line_end=start + len(rows) - 1,
            ))
            continue

        quote = _QUOTE_LINE.match(line)
        if quote:
            flush_paragraph()
            start = index
            quoted: list[str] = []
            while index < len(lines):
                matched = _QUOTE_LINE.match(lines[index])
                if not matched:
                    break
                quoted.append(matched.group(1))
                index += 1
            raws.append(_Raw(
                content="\n".join(quoted).strip(),
                block_type=BLOCK_QUOTE,
                line_start=start,
                line_end=start + len(quoted) - 1,
            ))
            continue

        item = _LIST_ITEM.match(line)
        if item:
            flush_paragraph()
            start = index
            body_lines = [line]
            index += 1
            # 列表项的续行（缩进更深的非空行）并入同一 Block，保留父子关联。
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip():
                    break
                if _LIST_ITEM.match(nxt) or _ATX_HEADING.match(nxt):
                    break
                if nxt.startswith((" ", "\t")):
                    body_lines.append(nxt)
                    index += 1
                    continue
                break
            raws.append(_Raw(
                content="\n".join(body_lines).rstrip(),
                block_type=BLOCK_LIST_ITEM,
                line_start=start,
                line_end=start + len(body_lines) - 1,
            ))
            continue

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if not paragraph:
            paragraph_start = index
        paragraph.append(line.rstrip())
        index += 1

    flush_paragraph()
    return raws


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_HEADING_STYLE = re.compile(
    r"^(?:heading|标题)\s*([1-6])$", re.IGNORECASE,
)


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_DOCX_NS}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_DOCX_NS}tab":
            parts.append("\t")
        elif node.tag in {f"{_DOCX_NS}br", f"{_DOCX_NS}cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _docx_heading_level(paragraph: ElementTree.Element) -> int:
    """从样式名或 outlineLvl 推断标题层级；不是标题时返回 0。"""
    properties = paragraph.find(f"{_DOCX_NS}pPr")
    if properties is None:
        return 0
    style = properties.find(f"{_DOCX_NS}pStyle")
    if style is not None:
        value = style.get(f"{_DOCX_NS}val", "")
        matched = _DOCX_HEADING_STYLE.match(value.strip())
        if matched:
            return int(matched.group(1))
    outline = properties.find(f"{_DOCX_NS}outlineLvl")
    if outline is not None:
        value = outline.get(f"{_DOCX_NS}val", "")
        if value.isdigit():
            level = int(value) + 1
            if 1 <= level <= 6:
                return level
    return 0


def _docx_blocks(data: bytes) -> list[_Raw]:
    """解析 DOCX：标题样式 → heading，表格 → table，其余非空段落 → paragraph。"""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    body = root.find(f"{_DOCX_NS}body")
    if body is None:
        return []
    raws: list[_Raw] = []
    for child in body:
        if child.tag == f"{_DOCX_NS}p":
            content = _docx_paragraph_text(child)
            if not content:
                continue
            level = _docx_heading_level(child)
            raws.append(_Raw(
                content=content,
                block_type=BLOCK_HEADING if level else BLOCK_PARAGRAPH,
                line_start=0,
                line_end=0,
                level=level,
            ))
        elif child.tag == f"{_DOCX_NS}tbl":
            rows: list[str] = []
            for row in child.findall(f"{_DOCX_NS}tr"):
                cells = [
                    _docx_paragraph_text(cell).replace("\n", " ")
                    for cell in row.findall(f"{_DOCX_NS}tc")
                ]
                if any(cell.strip() for cell in cells):
                    rows.append(" | ".join(cells))
            if rows:
                raws.append(_Raw(
                    content="\n".join(rows),
                    block_type=BLOCK_TABLE,
                    line_start=0,
                    line_end=0,
                ))
    return raws


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _pdf_blocks(data: bytes) -> list[_Raw]:
    """按页解析 PDF：页码写入 page_start/page_end，不推导标题层级。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted and reader.decrypt("") == 0:
        raise ValueError("encrypted_pdf_not_supported")
    raws: list[_Raw] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        for paragraph in re.split(r"\n\s*\n", text):
            content = paragraph.strip()
            if not content:
                continue
            raws.append(_Raw(
                content=content,
                block_type=BLOCK_PARAGRAPH,
                line_start=0,
                line_end=0,
                page_start=page_number,
                page_end=page_number,
            ))
    return raws


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

class _HTMLBlockParser(HTMLParser):
    """把 HTML 拆成结构块：h1-h6 / p / li / pre / table。"""

    _HEADINGS: ClassVar[dict[str, int]] = {
        "h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6,
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_Raw] = []
        self._hidden: int = 0
        self._buffer: list[str] = []
        self._tag: str | None = None
        self._level: int = 0
        self._table_depth: int = 0
        self._rows: list[str] = []
        self._row: list[str] = []
        self._cell: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden += 1
            return
        if tag == "table":
            self._table_depth += 1
            self._rows = []
            return
        if self._table_depth:
            if tag == "tr":
                self._row = []
            elif tag in {"td", "th"}:
                self._cell = []
            return
        if tag in self._HEADINGS:
            self._start_block(tag, self._HEADINGS[tag])
        elif tag in {"p", "li", "pre", "blockquote"}:
            self._start_block(tag, 0)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._hidden:
            self._hidden -= 1
            return
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._rows:
                self.blocks.append(_Raw(
                    content="\n".join(self._rows),
                    block_type=BLOCK_TABLE, line_start=0, line_end=0,
                ))
            self._rows = []
            return
        if self._table_depth:
            if tag in {"td", "th"}:
                self._row.append(" ".join("".join(self._cell).split()))
                self._cell = []
            elif tag == "tr":
                cells = [cell for cell in self._row if cell]
                if cells:
                    self._rows.append(" | ".join(cells))
                self._row = []
            return
        if tag in self._HEADINGS or tag in {"p", "li", "pre", "blockquote"}:
            self._finish_block()

    @override
    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        if self._table_depth:
            self._cell.append(data)
            return
        if self._tag is not None:
            self._buffer.append(data)

    def _start_block(self, tag: str, level: int) -> None:
        self._finish_block()
        self._tag = tag
        self._level = level
        self._buffer = []

    def _finish_block(self) -> None:
        if self._tag is None:
            return
        content = " ".join("".join(self._buffer).split())
        tag, level = self._tag, self._level
        self._tag = None
        self._buffer = []
        self._level = 0
        if not content:
            return
        if tag in self._HEADINGS:
            block_type = BLOCK_HEADING
        elif tag == "li":
            block_type = BLOCK_LIST_ITEM
        elif tag == "pre":
            block_type = BLOCK_CODE
        elif tag == "blockquote":
            block_type = BLOCK_QUOTE
        else:
            block_type = BLOCK_PARAGRAPH
        self.blocks.append(_Raw(
            content=content, block_type=block_type, line_start=0, line_end=0, level=level,
        ))

    @override
    def close(self) -> None:
        super().close()
        self._finish_block()


def _html_blocks(text: str) -> list[_Raw]:
    parser = _HTMLBlockParser()
    parser.feed(text)
    parser.close()
    return parser.blocks


# ---------------------------------------------------------------------------
# CSV / TSV / JSON / 源码 / 纯文本
# ---------------------------------------------------------------------------

def _csv_blocks(text: str) -> list[_Raw]:
    """首行作表头，整表作为一个 table Block（切分时复制表头）。"""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return []
    if len(lines) == 1:
        return [_Raw(content=lines[0], block_type=BLOCK_PARAGRAPH, line_start=0, line_end=0)]
    return [_Raw(
        content="\n".join(lines), block_type=BLOCK_TABLE,
        line_start=0, line_end=len(lines) - 1,
    )]


_CODE_BOUNDARY = re.compile(
    r"^(?:async\s+def|def|class|function|func|fn|impl|interface|enum|struct|"
    + r"public|private|protected|internal|export|module|namespace)\b"
)
_DECORATOR = re.compile(r"^\s*@")


def _code_blocks(text: str) -> list[_Raw]:
    """按顶层定义边界拆分源码；识别不到边界时整体作为一个 code Block。"""
    lines = text.split("\n")
    boundaries: list[int] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if line[0] not in {" ", "\t"} and _CODE_BOUNDARY.match(line):
            start = index
            # 紧邻的装饰器/注释归入下一个定义，避免把 @decorator 与 def 拆开。
            back = start - 1
            while back >= 0 and _DECORATOR.match(lines[back]):
                start = back
                back -= 1
            boundaries.append(start)
    if not boundaries or boundaries[0] != 0:
        boundaries.insert(0, 0)
    if len(boundaries) == 1:
        return [_Raw(
            content=text.rstrip(), block_type=BLOCK_CODE,
            line_start=0, line_end=max(0, len(lines) - 1),
        )]
    raws: list[_Raw] = []
    for position, start in enumerate(boundaries):
        end = boundaries[position + 1] - 1 if position + 1 < len(boundaries) else len(lines) - 1
        content = "\n".join(lines[start : end + 1]).strip("\n")
        if not content.strip():
            continue
        raws.append(_Raw(
            content=content, block_type=BLOCK_CODE, line_start=start, line_end=end,
        ))
    return raws


def _text_blocks(text: str) -> list[_Raw]:
    """纯文本：按空行分段，段内保留原始换行。"""
    raws: list[_Raw] = []
    lines = text.split("\n")
    start: int | None = None
    buffer: list[str] = []
    for index, line in enumerate(lines):
        if line.strip():
            if start is None:
                start = index
            buffer.append(line.rstrip())
        elif buffer:
            raws.append(_Raw(
                content="\n".join(buffer).strip(),
                block_type=BLOCK_TEXT, line_start=start or 0, line_end=index - 1,
            ))
            buffer = []
            start = None
    if buffer:
        raws.append(_Raw(
            content="\n".join(buffer).strip(),
            block_type=BLOCK_TEXT, line_start=start or 0, line_end=len(lines) - 1,
        ))
    return raws


def _generic_blocks(text: str) -> list[_Raw]:
    """兜底解析：按空行分段，任何非空行都会被保留。"""
    return _text_blocks(text)


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def _assign_sequential_lines(raws: list[_Raw]) -> None:
    """为没有真实行号的格式（DOCX/PDF/HTML）按重建文本顺序补行号。"""
    cursor = 0
    for raw in raws:
        raw.line_start = cursor
        raw.line_end = cursor + raw.content.count("\n")
        cursor = raw.line_end + 1


def _finalize(raws: list[_Raw], *, sequential_lines: bool) -> list[Block]:
    """套用标题层级、section_id、block_index，产出最终 Block 列表。"""
    if sequential_lines:
        _assign_sequential_lines(raws)
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    for raw in raws:
        # 只裁掉首尾空行与行尾空白：行首缩进是代码与嵌套列表的结构信息。
        content = raw.content.strip("\n").rstrip()
        if not content.strip():
            continue
        if raw.block_type == BLOCK_HEADING and raw.level:
            while stack and stack[-1][0] >= raw.level:
                stack.pop()
            stack.append((raw.level, content))
        path = tuple(text for _, text in stack)
        blocks.append(Block(
            content=content,
            block_type=raw.block_type,
            heading_path=path,
            block_index=len(blocks),
            line_start=raw.line_start,
            line_end=raw.line_end,
            page_start=raw.page_start,
            page_end=raw.page_end,
            section_id=section_id_for(path),
        ))
    return blocks


def parse_document_blocks(data: bytes, filename: str) -> list[Block]:
    """把原始字节解析为 Block 列表；任何格式失败都退回通用文本 Block。"""
    extracted = extract_document_bytes(data, filename)
    suffix = Path(filename).suffix.casefold()
    sequential = False
    try:
        if suffix in {".md", ".markdown"}:
            raws = _markdown_blocks(_decode_preserving_indent(data))
        elif suffix == ".docx":
            raws, sequential = _docx_blocks(data), True
        elif suffix == ".pdf":
            raws, sequential = _pdf_blocks(data), True
        elif suffix in {".html", ".htm"}:
            raws, sequential = _html_blocks(_decode_preserving_indent(data)), True
        elif suffix in {".csv", ".tsv"}:
            raws = _csv_blocks(extracted.text)
        elif suffix == ".json" or suffix in CODE_SUFFIXES:
            raws = _code_blocks(_decode_preserving_indent(data))
        else:
            raws = _text_blocks(_decode_preserving_indent(data))
    except Exception:
        logger.warning("结构化解析失败，退回通用文本 Block：%s", filename, exc_info=True)
        raws, sequential = _generic_blocks(_decode_preserving_indent(data)), False
    if not raws:
        raws = _generic_blocks(extracted.text)
    return _finalize(raws, sequential_lines=sequential)
