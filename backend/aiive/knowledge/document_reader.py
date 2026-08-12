"""安全的结构化文档文本提取器；从不执行文档内脚本或宏。"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, override
from xml.etree import ElementTree

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_CHARS = 2_000_000
SUPPORTED_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".html", ".htm", ".json", ".csv", ".tsv",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".docx", ".pdf",
})


class DocumentReadError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    doc_type: str
    mime_type: str
    metadata: dict[str, Any]


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden: int = 0
        self._parts: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg"}:
            self._hidden += 1
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._hidden:
            self._hidden -= 1
        elif tag in {"p", "div", "li", "tr"}:
            self._parts.append("\n")

    @override
    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self._parts.append(data)

    def text(self) -> str:
        return _clean_text(" ".join(self._parts))


def _clean_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 常见本地中文纯文本的可恢复兜底；二进制格式在进入本函数前已按后缀分流。
        try:
            return data.decode("gb18030")
        except UnicodeDecodeError as error:
            raise DocumentReadError("document_text_encoding_unsupported") from error


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, OSError, zipfile.BadZipFile) as error:
        raise DocumentReadError("invalid_docx") from error
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise DocumentReadError("invalid_docx_xml") from error
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{namespace}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{namespace}tab":
                parts.append("\t")
            elif node.tag in {f"{namespace}br", f"{namespace}cr"}:
                parts.append("\n")
        rendered = "".join(parts).strip()
        if rendered:
            paragraphs.append(rendered)
    return _clean_text("\n".join(paragraphs))


def _pdf_text(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - production dependency
        raise DocumentReadError("pdf_support_not_installed") from error
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise DocumentReadError("encrypted_pdf_not_supported")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except DocumentReadError:
        raise
    except Exception as error:
        raise DocumentReadError("invalid_pdf") from error
    return _clean_text("\n\n".join(page for page in pages if page)), len(reader.pages)


def extract_document_bytes(data: bytes, filename: str) -> ExtractedDocument:
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentReadError(f"document_too_large:{len(data)}")
    suffix = Path(filename).suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DocumentReadError(f"unsupported_document_type:{suffix or 'none'}")

    metadata: dict[str, Any] = {"source_name": Path(filename).name, "bytes": len(data)}
    if suffix == ".docx":
        text, doc_type = _docx_text(data), "docx"
        mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif suffix == ".pdf":
        text, pages = _pdf_text(data)
        metadata["pages"] = pages
        doc_type, mime_type = "pdf", "application/pdf"
    else:
        decoded = _decode_text(data)
        if suffix in {".html", ".htm"}:
            parser = _VisibleHTML()
            parser.feed(decoded)
            text, doc_type = parser.text(), "html"
        elif suffix == ".json":
            try:
                text = json.dumps(json.loads(decoded), ensure_ascii=False, indent=2)
            except json.JSONDecodeError as error:
                raise DocumentReadError("invalid_json_document") from error
            doc_type = "json"
        elif suffix in {".csv", ".tsv"}:
            delimiter = "\t" if suffix == ".tsv" else ","
            rows = csv.reader(io.StringIO(decoded), delimiter=delimiter)
            text = _clean_text("\n".join(" | ".join(cell.strip() for cell in row) for row in rows))
            doc_type = "table"
        else:
            text = _clean_text(decoded)
            doc_type = "markdown" if suffix in {".md", ".markdown"} else "code" if suffix in {
                ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
            } else "text"
        mime_type = mimetypes.guess_type(filename)[0] or "text/plain"

    if not text:
        raise DocumentReadError("document_has_no_extractable_text")
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS]
        metadata["truncated"] = True
    metadata["characters"] = len(text)
    metadata["lines"] = text.count("\n") + 1
    return ExtractedDocument(text=text, doc_type=doc_type, mime_type=mime_type, metadata=metadata)


def extract_document(path: str | Path) -> ExtractedDocument:
    source = Path(path)
    if not source.is_file():
        raise DocumentReadError("document_not_found")
    return extract_document_bytes(source.read_bytes(), source.name)
