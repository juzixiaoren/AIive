"""Bash 输出流式落盘：完整性、容量上限、续读位置与降级路径（离线，真实 bash）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiive.config import settings
from aiive.runtime.tool_output_guard import guard_tool_output
from aiive.runtime.tool_output_store import is_managed_path
from aiive.tools import local_shell
from aiive.tools.builtin_files import handle_read_text_file
from aiive.tools.local_shell import resolve_bash, run_bash


@pytest.fixture()
def output_dir(tmp_path, monkeypatch):
    target = tmp_path / "tool_outputs"
    target.mkdir()
    monkeypatch.setattr(settings, "tool_output_dir", str(target))
    return target


def _require_bash() -> None:
    if resolve_bash() is None:
        pytest.skip("本机未安装 bash，跳过真实执行用例")


def _log_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


# 1. 大输出完整落盘：内存只留预览，日志行数与总字节数精确
def test_large_output_streams_complete_content_to_log(output_dir, tmp_path) -> None:
    _require_bash()
    result = run_bash("seq 1 4000", cwd=str(tmp_path), timeout_seconds=60)
    assert result["ok"] is True
    log_path = result["stdout_log"]
    assert log_path and is_managed_path(log_path)
    text = _log_text(log_path)
    assert text.count("\n") == 4000
    assert result["stdout_bytes"] == len(text.encode("utf-8"))
    assert result["capture_truncated"]["stdout"] is False
    assert result["truncated"]["stdout"] is True
    # 内存里只有预览：等于日志前缀，且不超过配置的预览长度
    assert result["stdout"] == text[: len(result["stdout"])]
    assert len(result["stdout"]) == min(
        settings.shell_output_preview_chars, settings.shell_max_output_bytes,
    )


# 2. 预览位置可用现有分页工具续读，且不重复读取已看过的内容
def test_preview_position_continues_with_read_text_file(output_dir, tmp_path) -> None:
    _require_bash()
    result = run_bash("seq 1 4000", cwd=str(tmp_path), timeout_seconds=60)
    log_path = result["stdout_log"]
    page = handle_read_text_file(
        log_path,
        offset=result["stdout_next_offset"],
        limit=50,
        max_chars=1000,
        char_offset=result["stdout_next_char_offset"],
    )
    assert page["ok"] is True
    assert page["content"]
    # 续读页从预览结束的位置接着读：预览尾段 + 本页内容 = 日志中的连续片段
    preview = result["stdout"]
    tail = preview[len(preview) - result["stdout_next_char_offset"]:]
    assert page["content"].startswith(tail)
    assert tail + page["content"] in _log_text(log_path)
    assert page["line_range"][0] == result["stdout_next_offset"]


# 3. 日志容量上限：停止写盘并标记 capture_truncated，但仍报告真实总字节数
def test_log_capacity_marks_capture_truncated(output_dir, tmp_path, monkeypatch) -> None:
    _require_bash()
    monkeypatch.setattr(settings, "shell_max_log_bytes", 4096)
    result = run_bash("seq 1 5000", cwd=str(tmp_path), timeout_seconds=60)
    log_path = result["stdout_log"]
    assert log_path
    assert Path(log_path).stat().st_size <= 4096
    assert result["capture_truncated"]["stdout"] is True
    assert result["stdout_bytes"] > 4096


# 4. 日志不可用：退化为有界内存预览，明确标记 log_error，不丢结构化字段
def test_log_unavailable_degrades_to_bounded_memory(output_dir, tmp_path, monkeypatch) -> None:
    _require_bash()
    def _boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(local_shell, "StreamingLog", _boom)
    monkeypatch.setattr(settings, "shell_max_output_bytes", 4096)
    result = run_bash("seq 1 3000", cwd=str(tmp_path), timeout_seconds=60)
    assert result["ok"] is True
    assert result["stdout_log"] == ""
    assert result["log_error"] == "log_write_failed"
    assert result["stdout"].startswith("1\n2\n3")
    assert result["stdout_bytes"] > len(result["stdout"].encode("utf-8"))
    assert result["capture_truncated"]["stdout"] is True


# 5. stdout / stderr 同时大输出：两路都完整落盘，不发生管道阻塞
def test_stdout_and_stderr_both_streamed(output_dir, tmp_path) -> None:
    _require_bash()
    command = "for i in $(seq 1 2000); do echo out-$i; echo err-$i 1>&2; done"
    result = run_bash(command, cwd=str(tmp_path), timeout_seconds=60)
    assert result["ok"] is True
    assert result["stdout_bytes"] == result["stderr_bytes"]
    assert _log_text(result["stdout_log"]).count("\n") == 2000
    assert _log_text(result["stderr_log"]).count("\n") == 2000


# 6. 超时：已产生的输出不丢（内容在预览范围内时不留日志文件）
def test_timeout_keeps_partial_output(output_dir, tmp_path) -> None:
    _require_bash()
    result = run_bash("echo before-timeout; sleep 30", cwd=str(tmp_path), timeout_seconds=1)
    assert result["timed_out"] is True
    assert result["error_type"] == "timeout"
    assert "before-timeout" in result["stdout"]
    assert result["stdout_log"] == ""
    assert result["truncated"]["stdout"] is False


# 7. 守卫复用流式日志：不二次落盘，回填预览 + 总字节数 + 续读位置
def test_guard_reuses_streamed_log_without_second_write(output_dir, tmp_path) -> None:
    _require_bash()
    result = run_bash("seq 1 4000", cwd=str(tmp_path), timeout_seconds=60)
    before = {item.name for item in output_dir.iterdir()}
    guarded = guard_tool_output("run_shell", json.dumps(result))
    after = {item.name for item in output_dir.iterdir()}
    assert after == before
    assert guarded.reason == "bash_streamed_log"
    payload = json.loads(guarded.content)
    meta = payload["stdout_meta"]
    assert meta["log_path"] == result["stdout_log"]
    assert meta["total_bytes"] == result["stdout_bytes"]
    assert meta["capture_truncated"] is False
    assert meta["next_offset"] == result["stdout_next_offset"]
    assert meta["next_char_offset"] == result["stdout_next_char_offset"]
    assert "read_text_file" in payload["hint"]
    assert payload["exit_code"] == 0


# 8. 后台任务的更紧预算：结构完整且不超预算，日志路径仍然可用
def test_guard_budget_fits_worker_summary(output_dir, tmp_path) -> None:
    _require_bash()
    result = run_bash("seq 1 4000", cwd=str(tmp_path), timeout_seconds=60)
    guarded = guard_tool_output("run_shell", json.dumps(result), max_chars=1200)
    assert len(guarded.content) <= 1200
    payload = json.loads(guarded.content)
    assert payload["log_path"]
    assert payload["stdout_meta"]["capture_truncated"] is False
    assert payload["inline_trimmed"] is True


# 9. 小输出不留日志文件，也不改变回填行为
def test_small_output_stays_inline(output_dir, tmp_path) -> None:
    _require_bash()
    result = run_bash("echo hi", cwd=str(tmp_path), timeout_seconds=20)
    assert result["stdout"] == "hi\n"
    assert result["truncated"]["stdout"] is False
    assert result["stdout_log"] == ""
    assert list(output_dir.iterdir()) == []
    guarded = guard_tool_output("run_shell", json.dumps(result))
    assert guarded.offloaded is False
    assert json.loads(guarded.content)["stdout"] == "hi\n"