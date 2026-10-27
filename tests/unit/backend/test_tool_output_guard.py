"""工具结果回填预算：落盘、分页、边界、清理。全部离线，不调用外部服务。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiive.config import settings
from aiive.runtime.agent_graph import parse_tool_execution_receipt
from aiive.runtime.tool_output_guard import (
    BASH_HEAD_LINES,
    BASH_TAIL_LINES,
    INLINE_CHAR_LIMIT,
    guard_tool_output,
)
from aiive.runtime.tool_output_store import (
    cleanup_expired_tool_outputs,
    is_managed_path,
    save_tool_output,
)
from aiive.tools.builtin_files import handle_read_text_file


@pytest.fixture()
def output_dir(tmp_path, monkeypatch):
    target = tmp_path / "tool_outputs"
    target.mkdir()
    monkeypatch.setattr(settings, "tool_output_dir", str(target))
    return target


def _shell_payload(lines: int, width: int = 20) -> str:
    stdout = "\n".join(f"line-{i:05d}-" + "x" * width for i in range(lines))
    return json.dumps({"ok": True, "exit_code": 0, "stdout": stdout, "stderr": "", "timed_out": False})


# 1. 短结果原样回填
def test_short_result_unchanged(output_dir) -> None:
    payload = json.dumps({"ok": True, "result": "ok"})
    guarded = guard_tool_output("mcp_fake", payload)
    assert guarded.content == payload
    assert guarded.offloaded is False
    assert list(output_dir.iterdir()) == []


# 2. Bash 超过 1000 行：首 50 + 尾 50 + 总数 + 省略区间 + 日志
def test_bash_over_line_limit_offloads(output_dir) -> None:
    total = 1500
    guarded = guard_tool_output("run_shell", _shell_payload(total))
    payload = json.loads(guarded.content)
    assert guarded.offloaded is True
    assert payload["exit_code"] == 0
    assert payload["ok"] is True
    assert payload["stdout_meta"]["total_lines"] == total
    assert payload["stdout_meta"]["head_lines"] == BASH_HEAD_LINES
    assert payload["stdout_meta"]["tail_lines"] == BASH_TAIL_LINES
    assert payload["stdout_meta"]["omitted_lines"] == [BASH_HEAD_LINES, total - BASH_TAIL_LINES]
    assert "已省略" in payload["stdout"]
    assert payload["stdout"].splitlines()[0].startswith("line-00000")
    assert payload["stdout"].splitlines()[-1].startswith(f"line-{total - 1:05d}")
    log = Path(payload["log_path"])
    assert log.is_file()
    text = log.read_text(encoding="utf-8")
    assert f"line-{total - 1:05d}" in text
    assert text.count("\n") >= total


# 3. Bash 行数不多但单行极长：仍受统一字符预算限制
def test_bash_long_lines_bounded(output_dir) -> None:
    payload = json.dumps({"ok": True, "exit_code": 1, "stdout": "y" * 5000, "stderr": "z" * 5000})
    guarded = guard_tool_output("run_shell", payload)
    assert len(guarded.content) < 6000
    parsed = json.loads(guarded.content)
    assert parsed["exit_code"] == 1
    assert parsed["truncated"] is True
    assert parsed["log_path"]
    # 行数未超限 → 走统一字符预算（不重复首尾），省略区间是"单行内部被裁剪"以外的全部行
    assert parsed["stdout_meta"]["mode"] == "char_budget"
    assert parsed["stdout_meta"]["tail_lines"] == 0
    assert parsed["stdout_meta"]["omitted_lines"] == [1, 1]
    assert parsed["stderr_meta"]["mode"] == "char_budget"


# 4. 超长 MCP 结果：摘要 + 类型 + 完整长度 + 日志路径
def test_mcp_long_result_offloads(output_dir) -> None:
    big = {"items": [{"id": i, "text": "a" * 40} for i in range(120)], "ok": True}
    raw = json.dumps(big)
    guarded = guard_tool_output("mcp_fake_search", raw)
    assert len(raw) > INLINE_CHAR_LIMIT
    assert len(guarded.content) < INLINE_CHAR_LIMIT * 2
    parsed = json.loads(guarded.content)
    assert parsed["truncated"] is True
    assert parsed["result_type"] == "dict"
    assert parsed["full_length"] == len(raw)
    assert parsed["len_items"] == 120
    assert parsed["log_path"]
    log_text = Path(parsed["log_path"]).read_text(encoding="utf-8")
    assert log_text.startswith("# tool=mcp_fake_search")
    assert raw in log_text
    # 只回填摘要：不得包含完整原始结果
    assert raw not in guarded.content
    assert len(guarded.content) < 1500


# 5. 非 JSON 文本结果同样落盘
def test_text_result_offloads(output_dir) -> None:
    text = "traceback line\n" * 400
    guarded = guard_tool_output("mcp_fake_text", text)
    parsed = json.loads(guarded.content)
    assert parsed["result_type"] == "text"
    assert parsed["full_length"] == len(text)
    assert text in Path(parsed["log_path"]).read_text(encoding="utf-8")


# 6. 文件读取：超过 1000 字符不全文回填，返回路径/总数/分页提示
def test_read_document_offloads(output_dir) -> None:
    text = "段落内容\n" * 800
    payload = json.dumps({"ok": True, "path": "C:/docs/a.pdf", "doc_type": "pdf", "text": text})
    guarded = guard_tool_output("read_document", payload)
    parsed = json.loads(guarded.content)
    assert "text" not in parsed
    assert parsed["content_inlined"] is False
    assert parsed["path"] == "C:/docs/a.pdf"
    assert parsed["total_chars"] == len(text)
    assert parsed["total_lines"] == 800
    assert "read_text_file" in parsed["hint"]
    assert text in Path(parsed["log_path"]).read_text(encoding="utf-8")


# 7. 短文件读取保持原行为
def test_read_text_file_short_unchanged(output_dir) -> None:
    path = output_dir / "short.txt"
    path.write_text("hello\nworld\n", encoding="utf-8")
    result = handle_read_text_file(str(path))
    assert result["ok"] is True
    assert result["content"] == "hello\nworld"
    assert result["total_lines"] == 2
    assert result["has_more"] is False
    guarded = guard_tool_output("read_text_file", json.dumps(result))
    assert json.loads(guarded.content)["content"] == "hello\nworld"


# 8. 分页边界：offset/limit 与 next_offset
def test_read_text_file_pagination(output_dir) -> None:
    path = output_dir / "many.txt"
    path.write_text("\n".join(f"row-{i}" for i in range(200)), encoding="utf-8")
    first = handle_read_text_file(str(path), offset=0, limit=10)
    assert first["lines_read"] == 10
    assert first["next_offset"] == 10
    assert first["has_more"] is True
    assert len(first["content"]) <= 1000
    assert "offset=10" in first["hint"]
    second = handle_read_text_file(str(path), offset=first["next_offset"], limit=10)
    assert second["content"].splitlines()[0] == "row-10"
    last = handle_read_text_file(str(path), offset=195, limit=10)
    assert last["lines_read"] == 5
    assert last["next_offset"] is None
    assert last["has_more"] is False


# 9. 超长行：按字符续读，不撑爆上下文
def test_read_text_file_long_line_char_offset(output_dir) -> None:
    path = output_dir / "long.log"
    path.write_text("A" * 5000, encoding="utf-8")
    page = handle_read_text_file(str(path), max_chars=1000)
    assert page["line_truncated"] is True
    assert len(page["content"]) == 1000
    assert page["next_offset"] == 0
    assert page["next_char_offset"] == 1000
    assert "char_offset=1000" in page["hint"]
    tail = handle_read_text_file(str(path), char_offset=4000, max_chars=1000)
    assert tail["content"] == "A" * 1000


# 10. 路径安全：只允许 ~/Documents 与工具输出目录
def test_read_text_file_rejects_outside_path(output_dir, tmp_path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    result = handle_read_text_file(str(outside))
    assert result["ok"] is False
    assert result["error_type"] == "access_denied"
    assert is_managed_path(str(output_dir / "x.log")) is True
    assert is_managed_path(str(outside)) is False


# 11. 落盘失败：降级为有界预览，绝不回填完整大结果
def test_save_failure_falls_back_bounded(output_dir, monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("aiive.runtime.tool_output_guard.save_tool_output", _boom)
    raw = json.dumps({"ok": True, "result": "z" * 5000})
    guarded = guard_tool_output("mcp_fake", raw)
    parsed = json.loads(guarded.content)
    assert parsed["offload_error"] == "log_write_failed"
    assert parsed["full_length"] == len(raw)
    assert len(guarded.content) < 2000
    # 预览最多 PREVIEW_CHARS 字符，不得回填完整大结果
    assert "z" * 400 not in guarded.content


# 12. 重复调用：各自独立落盘，回填内容稳定且仍有界
def test_repeated_calls_are_stable(output_dir) -> None:
    raw = json.dumps({"ok": True, "blob": "q" * 3000})
    first = guard_tool_output("mcp_fake", raw)
    second = guard_tool_output("mcp_fake", raw)
    first_payload = json.loads(first.content)
    second_payload = json.loads(second.content)
    first_payload.pop("log_path")
    second_payload.pop("log_path")
    assert first_payload == second_payload
    assert first.log_path != second.log_path
    assert len(list(output_dir.glob("*.log"))) == 2
    assert parse_tool_execution_receipt(first.content).ok is True


# 13. 结构化字段保留：错误信息与退出码不得丢失
def test_error_fields_preserved(output_dir) -> None:
    raw = json.dumps({"ok": False, "error": "boom", "error_type": "execution_failed", "exit_code": 2, "stdout": "e" * 4000})
    guarded = guard_tool_output("run_shell", raw)
    parsed = json.loads(guarded.content)
    assert parsed["ok"] is False
    assert parsed["error"] == "boom"
    assert parsed["error_type"] == "execution_failed"
    assert parsed["exit_code"] == 2
    receipt = parse_tool_execution_receipt(guarded.content)
    assert receipt.ok is False
    assert receipt.error_type == "execution_failed"


# 14. 过期清理：只删本目录内超期日志，保留新日志与其他文件
def test_cleanup_removes_only_expired(output_dir) -> None:
    old = output_dir / "old.log"
    fresh = output_dir / "fresh.log"
    other = output_dir / "notes.txt"
    for item in (old, fresh, other):
        item.write_text("x", encoding="utf-8")
    stale = (datetime.now(timezone.utc) - timedelta(hours=11)).timestamp()
    os.utime(old, (stale, stale))
    os.utime(other, (stale, stale))
    removed = cleanup_expired_tool_outputs()
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert other.exists()


# 15. 进程重启：日志仍在，可按页读回原始内容
def test_log_survives_restart_and_is_readable(output_dir) -> None:
    text = "\n".join(f"payload-{i}" for i in range(500))
    guarded = guard_tool_output("mcp_fake", json.dumps({"ok": True, "data": text}))
    log_path = json.loads(guarded.content)["log_path"]
    assert Path(log_path).is_file()
    page = handle_read_text_file(log_path, offset=0, limit=20, max_chars=800)
    assert page["ok"] is True
    assert page["total_lines"] >= 1
    assert page["lines_read"] >= 1
    # 新实例（模拟重启后）仍能读到同一文件
    assert cleanup_expired_tool_outputs() == 0
    assert Path(log_path).exists()


# 16. 清理不触碰专用目录之外的文件
def test_cleanup_ignores_outside_files(output_dir, tmp_path) -> None:
    outside = tmp_path / "keep.log"
    outside.write_text("keep", encoding="utf-8")
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    os.utime(outside, (stale, stale))
    assert cleanup_expired_tool_outputs() == 0
    assert outside.exists()


# 17. 落盘文件唯一且原子：不产生残留临时文件
def test_save_is_atomic_and_unique(output_dir) -> None:
    first = save_tool_output("a", "run_shell")
    second = save_tool_output("a", "run_shell")
    assert first != second
    assert first.read_text(encoding="utf-8") == "a"
    assert list(output_dir.glob(".tmp-*")) == []


# 18. 接线守卫：_tools_node 必须在构造回执前调用统一入口
def test_tools_node_wires_guard() -> None:
    source = (Path(__file__).resolve().parents[3] / "backend/aiive/runtime/agent_graph.py").read_text(encoding="utf-8")
    node = source.split("def _tools_node", 1)[1].split("return {", 1)[0]
    assert "guard_tool_output(" in node
    assert node.index("guard_tool_output(") < node.index("parse_tool_execution_receipt(")


# 19. 大文件（> 5MB）仍可翻页读到末尾：不再有整文件扫描上限
def test_large_file_pages_past_old_scan_cap(output_dir) -> None:
    path = output_dir / "huge.log"
    total = 300_000
    with open(path, "w", encoding="utf-8") as handle:
        for i in range(total):
            handle.write(f"row-{i:06d}-{'x' * 12}\n")
    assert path.stat().st_size > 5 * 1024 * 1024
    first = handle_read_text_file(str(path), offset=0, limit=5)
    assert first["total_lines"] == total
    tail = handle_read_text_file(str(path), offset=total - 3, limit=5)
    assert tail["lines_read"] == 3
    assert tail["content"].splitlines()[0] == f"row-{total - 3:06d}-{'x' * 12}"
    assert tail["next_offset"] is None
    assert tail["has_more"] is False


# 20. 实际读取范围必须与 next_offset 一致（逐页回溯校验）
def test_page_range_matches_next_offset(output_dir) -> None:
    path = output_dir / "var.txt"
    lines = [f"l{i}-" + "y" * (i % 17) for i in range(400)]
    path.write_text("\n".join(lines), encoding="utf-8")
    offset = 0
    seen: list[str] = []
    for _ in range(200):
        page = handle_read_text_file(str(path), offset=offset, limit=7, max_chars=200)
        page_lines = page["content"].splitlines() if page["content"] else []
        assert page["lines_read"] == len(page_lines)
        assert page["line_range"] == [offset, offset + len(page_lines) - 1]
        assert page_lines == lines[offset:offset + len(page_lines)]
        seen.extend(page_lines)
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + len(page_lines)
        offset = page["next_offset"]
    assert seen == lines


# 21. 符号链接逃逸：指向目录外的链接必须被拒绝
def test_symlink_escape_rejected(output_dir, tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = output_dir / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不支持创建符号链接")
    result = handle_read_text_file(str(link))
    assert result["ok"] is False
    assert result["error_type"] == "access_denied"


# 22. .. 穿越：拼接上级目录的路径必须被拒绝
def test_dotdot_traversal_rejected(output_dir, tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    crafted = os.path.join(str(output_dir), "..", "secret.txt")
    result = handle_read_text_file(crafted)
    assert result["ok"] is False
    assert result["error_type"] == "access_denied"


# 23. MCP 注册表信封 {ok, result:{...}}：同样只回填摘要
def test_mcp_envelope_result_summarized(output_dir) -> None:
    inner = {"rows": [{"id": i, "blob": "b" * 30} for i in range(200)]}
    raw = json.dumps({"ok": True, "result": inner})
    guarded = guard_tool_output("mcp_fake_query", raw)
    parsed = json.loads(guarded.content)
    assert parsed["ok"] is True
    assert parsed["result_type"] == "dict"
    assert parsed["full_length"] == len(raw)
    assert parsed["keys_result"] == ["rows"]
    assert raw not in guarded.content
    assert Path(parsed["log_path"]).read_text(encoding="utf-8").endswith(raw)


# 24. Bash 行数超限 + 超长行：首尾各 50 行必须完整回填，省略区间正确
def test_bash_long_lines_keep_head_and_tail(output_dir) -> None:
    total = 1200
    wide = 5000
    stdout = "\n".join(f"L{i:05d}" + "w" * wide for i in range(total))
    raw = json.dumps({"ok": True, "exit_code": 0, "stdout": stdout, "stderr": ""})
    guarded = guard_tool_output("run_shell", raw)
    parsed = json.loads(guarded.content)
    meta = parsed["stdout_meta"]
    assert meta["mode"] == "head_tail"
    assert meta["head_lines"] == BASH_HEAD_LINES
    assert meta["tail_lines"] == BASH_TAIL_LINES
    assert meta["omitted_lines"] == [BASH_HEAD_LINES, total - BASH_TAIL_LINES]
    body = parsed["stdout"].splitlines()
    head_part = body[:BASH_HEAD_LINES]
    tail_part = body[-BASH_TAIL_LINES:]
    assert head_part[0].startswith("L00000")
    assert head_part[-1].startswith(f"L{BASH_HEAD_LINES - 1:05d}")
    assert tail_part[0].startswith(f"L{total - BASH_TAIL_LINES:05d}")
    assert tail_part[-1].startswith(f"L{total - 1:05d}")
    # 单行被裁剪到 200 字符（含裁剪标记），整段体积有界
    assert all(len(line) <= 200 + 32 for line in head_part + tail_part)
    assert len(parsed["stdout"]) < 50_000
    log_text = Path(parsed["log_path"]).read_text(encoding="utf-8")
    assert f"L{total - 1:05d}" in log_text


# 25. 行内续读：line_range / char_range / next_char_offset 语义一致
def test_char_range_semantics(output_dir) -> None:
    path = output_dir / "digits.log"
    digits = "0123456789" * 800  # 8000 字符单行
    path.write_text(digits, encoding="utf-8")
    page = handle_read_text_file(str(path), max_chars=1000)
    assert page["line_truncated"] is True
    assert page["line_range"] == [0, 0]
    assert page["char_range"] == [0, 1000]
    assert page["content"] == digits[page["char_range"][0]:page["char_range"][1]]
    assert page["next_offset"] == page["line_range"][0]
    assert page["next_char_offset"] == page["char_range"][1]
    nxt = handle_read_text_file(str(path), char_offset=page["next_char_offset"], max_chars=1000)
    assert nxt["char_range"] == [1000, 2000]
    assert nxt["content"] == digits[1000:2000]
    assert nxt["next_char_offset"] == 2000


# 26. 非截断分页：不返回 char_range，line_range 与 next_offset 严格衔接
def test_multi_line_page_has_no_char_range(output_dir) -> None:
    path = output_dir / "plain.txt"
    lines = [f"v{i}" for i in range(30)]
    path.write_text("\n".join(lines), encoding="utf-8")
    page = handle_read_text_file(str(path), offset=3, limit=5)
    assert "char_range" not in page
    assert page["line_range"] == [3, 7]
    assert page["content"] == "\n".join(lines[3:8])
    assert page["next_offset"] == page["line_range"][1] + 1
