"""Bounded path, symbol and content discovery in a project workspace."""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from aiive.source_snapshot import excluded_source_path

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 40_000
MAX_TOTAL_CONTEXT_CHARS = 120_000
MAX_SCAN_FILES = 20_000
MAX_SCAN_BYTES = 1_000_000
MAX_MATCHES = 200
MAX_MATCHES_IN_PROMPT = 30
MAX_CONTEXT_FILES = 8

TEXT_SUFFIXES: frozenset[str] = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".toml", ".yaml", ".yml",
    ".md", ".txt", ".css", ".html", ".sql", ".ini", ".cfg", ".sh",
})

# 关键词提取时忽略的通用词，避免把 "the/file/test" 当成检索目标。
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "when",
    "then", "than", "have", "has", "not", "but", "all", "any", "can",
    "should", "would", "could", "will", "must", "file", "files", "code",
    "test", "tests", "fix", "add", "update", "change", "make", "use",
    "using", "backend", "frontend", "aiive", "selfdev", "python", "function",
    "class", "method", "module", "return", "import", "true", "false", "none",
})


@dataclass
class ContextFile:
    """一个被纳入上下文的仓库文件。"""

    path: str
    content: str
    truncated: bool
    bytes: int
    reason: str
    next_byte_offset: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "path": self.path,
            "content": self.content,
            "truncated": self.truncated,
            "bytes": self.bytes,
            "reason": self.reason,
            "next_byte_offset": self.next_byte_offset,
        }


@dataclass
class ProjectContext:
    """一次代码理解的产物：文件内容 + 命中行 + 关键词。"""

    files: list[ContextFile] = field(default_factory=list)
    matches: list[dict[str, Any]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        """是否有文件因体积上限被截断。"""
        return any(item.truncated for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典（供记录与调试）。"""
        return {
            "files": [item.to_dict() for item in self.files],
            "matches": list(self.matches),
            "keywords": list(self.keywords),
            "truncated": self.truncated,
        }


class ProjectReader:
    """仓库范围内的只读、有界代码读取器。"""

    def __init__(
        self,
        repo_root: Path | None = None,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_files: int = MAX_CONTEXT_FILES,
        max_matches: int = MAX_MATCHES,
    ) -> None:
        if repo_root is None:
            from aiive.project_paths import source_root

            repo_root = source_root()
        self._root: Path = repo_root.resolve()
        self._max_file_bytes: int = max(1, int(max_file_bytes))
        self._max_files: int = max(1, int(max_files))
        self._max_matches: int = max(1, int(max_matches))

    @property
    def repo_root(self) -> Path:
        """返回读取器的仓库根目录。"""
        return self._root

    # ── 路径与过滤 ────────────────────────────────────────────────

    def _resolve(self, value: str) -> Path:
        """把仓库相对路径解析为绝对路径，拒绝越界与穿越。"""
        raw = str(value).strip().replace("\\", "/")
        if not raw:
            raise ValueError("empty path")
        relative = PurePosixPath(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid repo path: {value}")
        resolved = (self._root / relative).resolve()
        if not resolved.is_relative_to(self._root):
            raise ValueError(f"path escapes repo root: {value}")
        if self._is_denied(PurePosixPath(resolved.relative_to(self._root).as_posix())):
            raise ValueError(f"excluded repo path: {value}")
        return resolved

    @staticmethod
    def _relative(path: Path, root: Path) -> str:
        """返回 POSIX 风格的仓库相对路径。"""
        return path.relative_to(root).as_posix()

    @staticmethod
    def _is_denied(relative: PurePosixPath) -> bool:
        """判断路径是否属于禁止读取的目录/文件。"""
        return excluded_source_path(relative)

    def _iter_files(self) -> Iterator[tuple[str, Path]]:
        """遍历仓库内可读的文本文件，跳过禁止目录与超大文件。"""
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = sorted(
                name for name in dirnames
                if not self._is_denied(PurePosixPath((Path(dirpath) / name / "_").relative_to(self._root).as_posix()))
            )
            for name in sorted(filenames):
                if scanned >= MAX_SCAN_FILES:
                    logger.warning("代码上下文扫描达到文件上限 %s", MAX_SCAN_FILES)
                    return
                path = Path(dirpath) / name
                try:
                    relative = PurePosixPath(self._relative(path, self._root))
                except ValueError:
                    continue
                try:
                    self._resolve(relative.as_posix())
                except (ValueError, OSError, RuntimeError):
                    continue
                if path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                try:
                    if path.stat().st_size > MAX_SCAN_BYTES:
                        continue
                except OSError:
                    continue
                scanned += 1
                yield relative.as_posix(), path

    # ── 读取与搜索 ────────────────────────────────────────────────

    def read(self, relative_path: str, *, byte_offset: int = 0) -> ContextFile | None:
        """读取单个仓库文件（有界）；越界、被拒绝或不存在时返回 None。"""
        try:
            path = self._resolve(relative_path)
        except ValueError:
            return None
        normalized = PurePosixPath(str(relative_path).replace("\\", "/"))
        if self._is_denied(normalized) or not path.is_file():
            return None
        try:
            size = path.stat().st_size
            with path.open("rb") as stream:
                stream.seek(byte_offset)
                data = stream.read(self._max_file_bytes + 1)
        except OSError:
            logger.debug("读取仓库文件失败：%s", relative_path, exc_info=True)
            return None
        truncated = len(data) > self._max_file_bytes
        if truncated:
            data = data[: self._max_file_bytes]
            # Keep the next page on a UTF-8 codepoint boundary.
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                if error.end == len(data) and error.start > 0 and error.reason == "unexpected end of data":
                    data = data[:error.start]
        return ContextFile(
            path=normalized.as_posix(),
            content=data.decode("utf-8", errors="replace"),
            truncated=truncated,
            bytes=size,
            reason="read",
            next_byte_offset=byte_offset + len(data) if truncated else None,
        )

    def search_paths(self, query: str, *, limit: int | None = None) -> list[str]:
        normalized = query.replace("\\", "/").removeprefix("./")
        if not normalized:
            return []
        try:
            exact = self._resolve(normalized)
        except ValueError:
            exact = None
        if exact is not None and exact.is_file() and not self._is_denied(PurePosixPath(normalized)):
            return [normalized]
        from itertools import islice

        return list(islice((name for name, _ in self._iter_files() if normalized.casefold() in name.casefold()),
                           limit or self._max_matches))

    def search_symbols(self, symbol: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.search([symbol], limit=limit, exact_word=True)

    def search(
        self, keywords: list[str], *, limit: int | None = None, exact_word: bool = False
    ) -> list[dict[str, Any]]:
        """在仓库文本文件中做大小写不敏感的子串检索。"""
        needles = [item for item in keywords if item]
        if not needles:
            return []
        patterns = [re.compile(rf"\b{re.escape(item)}\b" if exact_word else re.escape(item), re.IGNORECASE)
                    for item in needles]
        cap = max(1, int(limit or self._max_matches))
        results: list[dict[str, Any]] = []
        for relative, path in self._iter_files():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if any(pattern.search(line) for pattern in patterns):
                    results.append({
                        "path": relative,
                        "line": line_number,
                        "text": line.strip()[:200],
                    })
                    if len(results) >= cap:
                        return results
        return results

    # ── 上下文收集 ────────────────────────────────────────────────

    @staticmethod
    def _keywords(goal: str) -> list[str]:
        """从目标描述中提取文件路径与标识符，作为检索关键词。"""
        ordered: list[str] = []
        seen: set[str] = set()

        def push(value: str) -> None:
            key = value.strip()
            if not key or key.casefold() in _STOPWORDS or key in seen:
                return
            seen.add(key)
            ordered.append(key)

        for match in re.findall(
            r"[A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|json|toml|md|ya?ml)", goal
        ):
            push(match)
        for match in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", goal):
            push(match)
        if not ordered:
            # 目标是纯中文时，退化为中文短语检索（命中注释/文档）。
            for match in re.findall(r"[\u4e00-\u9fff]{2,8}", goal):
                push(match)
        return ordered[:20]

    def _related_tests(self, relative_path: str) -> list[str]:
        """按文件名推断相关测试文件（最多两个）。"""
        path = PurePosixPath(relative_path)
        if path.suffix != ".py" or not relative_path.startswith("backend/aiive/"):
            return []
        stem = path.stem
        found: list[str] = []
        for candidate in (
            f"tests/unit/backend/test_{stem}.py",
            f"tests/integration/backend/test_{stem}.py",
        ):
            if (self._root / candidate).is_file():
                found.append(candidate)
        for base in ("tests/unit/backend", "tests/integration/backend"):
            directory = self._root / base
            if not directory.is_dir():
                continue
            for item in sorted(directory.glob(f"test_{stem}_*.py")):
                found.append(item.relative_to(self._root).as_posix())
        return found[:2]

    def collect_context(
        self, goal: str, *, explicit_files: list[str] | None = None
    ) -> ProjectContext:
        """根据目标收集相关代码、命中行与相关测试。"""
        keywords = self._keywords(goal)
        path_matches = [path for word in keywords if "." in word for path in self.search_paths(word, limit=5)]
        explicit = [item for item in (explicit_files or []) if item]
        matches = self.search(keywords, limit=self._max_matches) if keywords else []

        selected: list[str] = []
        if explicit:
            selected = explicit[: self._max_files]
        elif path_matches:
            selected = list(dict.fromkeys(path_matches))[: self._max_files]
        elif matches:
            counts = Counter(str(item["path"]) for item in matches)
            selected = [
                path for path, _count in sorted(
                    counts.items(),
                    key=lambda pair: (
                        -pair[1],
                        "tests/" in pair[0],
                        pair[0],
                    ),
                )
            ][: self._max_files]

        files: list[ContextFile] = []
        seen_paths: set[str] = set()
        for relative in selected:
            item = self.read(relative)
            if item is None or item.path in seen_paths:
                continue
            item.reason = "explicit" if explicit else "search"
            seen_paths.add(item.path)
            files.append(item)

        for relative in list(selected):
            if len(files) >= self._max_files * 2:
                break
            for test_path in self._related_tests(relative):
                if test_path in seen_paths:
                    continue
                item = self.read(test_path)
                if item is None:
                    continue
                item.reason = "related_test"
                seen_paths.add(item.path)
                files.append(item)

        return ProjectContext(
            files=files,
            matches=matches[:MAX_MATCHES_IN_PROMPT],
            keywords=keywords,
        )


def format_context_for_prompt(context: ProjectContext | dict[str, Any]) -> str:
    """把代码上下文渲染成提示词片段（有界、确定性）。"""
    if isinstance(context, ProjectContext):
        payload = context.to_dict()
    else:
        payload = context
    files = payload.get("files")
    matches = payload.get("matches")
    parts: list[str] = []
    total = 0
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            content = str(item.get("content") or "")
            block = f"## 文件：{path}\n<<<CODE\n{content}\nCODE>>>\n"
            if total + len(block) > MAX_TOTAL_CONTEXT_CHARS:
                parts.append("（上下文达到长度上限，剩余文件未展开）")
                break
            parts.append(block)
            total += len(block)
    if isinstance(matches, list) and matches:
        lines = [
            f"- {item.get('path')}:{item.get('line')}: {item.get('text')}"
            for item in matches[:MAX_MATCHES_IN_PROMPT]
            if isinstance(item, dict)
        ]
        if lines:
            parts.append("## 相关匹配\n" + "\n".join(lines))
    if not parts:
        return (
            "（未收集到相关代码。如果缺少必要信息，请把操作标记为 "
            "not_allowed_yet 并在 risk_notes 说明缺少什么，不要凭空猜测现有文件内容。）"
        )
    return "\n".join(parts)
