from __future__ import annotations

import base64
import codecs
import hashlib
import os
import re
import stat
import subprocess
import tempfile
from abc import ABC
from pathlib import Path
from typing import Any, override

from aiive.context.run_context import RunContext
from aiive.tools.base import BaseTool, ToolError, ToolExecutionContext


def workspace_path(ctx: RunContext | None, path: str = ".") -> Path:
    if not ctx or not ctx.task_workspace:
        raise ValueError("task_workspace_required")
    root = Path(ctx.task_workspace).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path_outside_task_workspace")
    return target


def _decode(data: bytes, encoding: str | None) -> tuple[str, str, bytes]:
    for bom, codec in (
        (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(bom):
            return data[len(bom):].decode(codec), codec, bom
    codec = encoding or "utf-8"
    return data.decode(codec), codec, b""


def read_page(
    path: Path, *, start_line: int = 1, end_line: int | None = None,
    char_offset: int = 0, byte_offset: int | None = None, byte_length: int = 8000,
    limit: int = 8000, encoding: str | None = None,
) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = stream.read()
        info = os.fstat(stream.fileno())
    result = {"path": str(path), "size": len(data), "mtime_ns": info.st_mtime_ns,
              "sha256": hashlib.sha256(data).hexdigest()}
    if byte_offset is not None:
        chunk = data[byte_offset:byte_offset + byte_length]
        next_offset = byte_offset + len(chunk)
        return {**result, "byte_offset": byte_offset, "next_byte_offset": next_offset,
                "content_base64": base64.b64encode(chunk).decode("ascii"),
                "complete": next_offset >= len(data), "eof": next_offset >= len(data)}
    try:
        content, codec, _ = _decode(data, encoding)
    except UnicodeDecodeError:
        return {**result, "ok": False, "error": "Use byte_offset or specify the file encoding"}
    lines = content.splitlines(keepends=True)
    selected = lines[start_line - 1:end_line]
    text = "".join(selected)[char_offset:]
    page = text[:limit]
    consumed = char_offset + len(page)
    line = start_line
    for item in selected:
        if consumed < len(item):
            break
        consumed -= len(item)
        line += 1
    complete = len(page) == len(text)
    return {**result, "content": page, "encoding": codec, "start_line": start_line,
            "line_count": len(lines), "complete": complete,
            "eof": complete and (end_line is None or end_line >= len(lines)),
            "next_start_line": line, "next_char_offset": consumed}


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _newline(text: str) -> str:
    match = re.search(r"\r\n|\n|\r", text)
    return match.group() if match else "\n"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            if mode is not None:
                temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class ReadFileTool(BaseTool):
    @override
    def _run(self, path: str, ctx: RunContext | None = None, **ranges: Any) -> dict[str, Any]:
        return read_page(workspace_path(ctx, path), **ranges)


class WorkspaceMutationTool(BaseTool, ABC):
    @override
    def normalize_error(self, error: Exception, context: ToolExecutionContext) -> ToolError:
        if isinstance(error, (OSError, ValueError, LookupError)):
            return ToolError("workspace_file_error", str(error), retry_hint="correct_arguments")
        return super().normalize_error(error, context)


class EditFileTool(WorkspaceMutationTool):
    @override
    def _run(
        self, path: str, old_text: str, new_text: str, expected_sha256: str = "",
        encoding: str | None = None, ctx: RunContext | None = None,
    ) -> dict[str, Any]:
        target = workspace_path(ctx, path)
        for _ in range(3):
            original = target.read_bytes()
            observed = hashlib.sha256(original).hexdigest()
            text, codec, bom = _decode(original, encoding)
            # Match line endings without normalizing untouched bytes in the file.
            needle = _normalize(old_text)
            pattern = r"(?:\r\n|\n|\r)".join(re.escape(line) for line in needle.split("\n"))
            matches = list(re.finditer(pattern, text)) if needle else []
            if len(matches) != 1:
                return {"ok": False, "error_type": "edit_conflict",
                        "error": "Expected one exact match in the current file. Read it and select a unique anchor.",
                        "sha256": observed, "matches": len(matches)}
            match = matches[0]
            replacement = _normalize(new_text).replace("\n", _newline(match.group() or text))
            if "\n" not in needle:
                replacement = _normalize(new_text).replace("\n", _newline(text))
            data = bom + (text[:match.start()] + replacement + text[match.end():]).encode(codec)
            if target.read_bytes() != original:
                continue
            if data != original:
                _atomic_write(target, data)
            return {"path": path, "sha256": hashlib.sha256(data).hexdigest(),
                    "changed": data != original, "rebased": bool(expected_sha256 and expected_sha256 != observed)}
        return {"ok": False, "error_type": "edit_conflict", "error": "File changed during edit. Read the current file and retry."}


def write_workspace_file(
    path: str, content: str, encoding: str | None = None,
    expected_sha256: str = "", ctx: RunContext | None = None,
) -> dict[str, Any]:
    target = workspace_path(ctx, path)
    exists = target.exists()
    original = target.read_bytes() if exists else b""
    if expected_sha256 and hashlib.sha256(original).hexdigest() != expected_sha256:
        return {"ok": False, "error_type": "edit_conflict", "error": "Read the current file before overwriting it."}
    text, codec, bom = _decode(original, encoding)
    data = bom + _normalize(content).replace("\n", _newline(text)).encode(codec)
    changed = not exists or data != original
    if changed:
        _atomic_write(target, data)
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "changed": changed}


class WriteFileTool(WorkspaceMutationTool):
    @override
    def _run(
        self, path: str, content: str, encoding: str | None = None,
        expected_sha256: str = "", ctx: RunContext | None = None,
    ) -> dict[str, Any]:
        return write_workspace_file(path, content, encoding, expected_sha256, ctx)


class ApplyPatchTool(WorkspaceMutationTool):
    @override
    def _run(self, patch: str, strip: int | None = None, ctx: RunContext | None = None) -> dict[str, Any]:
        cwd = workspace_path(ctx)
        diff = patch
        if strip is None:
            strip = int(bool(re.search(r"^(?:diff --git a/|--- a/|\+\+\+ b/)", diff, re.M)))
        proc = subprocess.run(
            ["git", "apply", f"-p{strip}", "--recount", "--whitespace=nowarn", "-"],
            input=(diff.rstrip("\n") + "\n").encode("utf-8"), cwd=cwd,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(cwd.parent)},
            capture_output=True, timeout=30,
        )
        return {"ok": proc.returncode == 0, "changed": proc.returncode == 0,
                "exit_code": proc.returncode, "error": proc.stderr.decode("utf-8", errors="replace")}
