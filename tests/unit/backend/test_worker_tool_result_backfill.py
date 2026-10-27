"""后台任务工具结果回填：Evidence 摘要复用统一守卫，大结果落盘并可续读（离线）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiive.config import settings
from aiive.db.models import Thread
from aiive.runtime.tool_output_guard import guard_tool_output
from aiive.runtime.tool_output_store import is_managed_path
from aiive.task_runtime.evidence import SUMMARY_LIMIT, EvidenceStore
from aiive.task_runtime.repository import TaskRepository
from aiive.tools.builtin_files import handle_read_text_file


@pytest.fixture()
def output_dir(tmp_path, monkeypatch):
    target = tmp_path / "tool_outputs"
    target.mkdir()
    monkeypatch.setattr(settings, "tool_output_dir", str(target))
    return target


def _action(db_session, capability_id: str = "run_shell"):
    thread = Thread(title="backfill")
    db_session.add(thread)
    db_session.flush()
    repo = TaskRepository(db_session)
    task = repo.create_task(
        thread_id=thread.id, goal="回填工具结果", task_brief={"goal": "回填工具结果"}, enqueue=False,
    )
    _, run = repo.start_run(task.id, trigger="dispatch")
    action = repo.create_action(
        task=task, run=run, capability_id=capability_id, arguments={},
        descriptor_hash="descriptor", risk_level="low", requires_approval=False,
    )
    return task, run, action


def _shell_result(lines: int) -> dict:
    return {
        "ok": True,
        "exit_code": 0,
        "stdout": "\n".join(f"line-{index:05d}" for index in range(lines)),
        "stderr": "",
        "timed_out": False,
    }


# 1. 大结果：Evidence 摘要走统一守卫，落盘 + 结构化摘要，可在预算内
def test_large_result_summary_is_guarded_and_offloaded(db_session, output_dir) -> None:
    task, run, action = _action(db_session)
    evidence = EvidenceStore().capture_result(
        db_session, task_id=task.id, run_id=run.id, action=action, result=_shell_result(3000),
    )
    summary = json.loads(evidence.summary)
    assert summary["ok"] is True
    assert summary["exit_code"] == 0
    assert summary["stdout_meta"]["total_lines"] == 3000
    assert len(evidence.summary) <= SUMMARY_LIMIT
    log_path = summary["log_path"]
    assert log_path and is_managed_path(log_path)
    log_text = Path(log_path).read_text(encoding="utf-8")
    assert "line-02999" in log_text


# 2. 小结果：摘要保持原样，不产生日志文件
def test_small_result_summary_is_unchanged(db_session, output_dir) -> None:
    task, run, action = _action(db_session)
    payload = {"ok": True, "stdout": "hi\n", "stderr": "", "exit_code": 0}
    evidence = EvidenceStore().capture_result(
        db_session, task_id=task.id, run_id=run.id, action=action, result=payload,
    )
    assert evidence.summary == json.dumps(payload, ensure_ascii=False, default=str)
    assert list(output_dir.iterdir()) == []


# 3. 失败结果：错误字段与退出码在摘要里必须保留
def test_error_fields_survive_summary(db_session, output_dir) -> None:
    task, run, action = _action(db_session)
    payload = {
        "ok": False,
        "exit_code": 2,
        "error_type": "nonzero_exit",
        "error": "command failed",
        "stdout": "x" * 8000,
        "stderr": "boom\n" * 500,
    }
    evidence = EvidenceStore().capture_result(
        db_session, task_id=task.id, run_id=run.id, action=action, result=payload,
    )
    summary = json.loads(evidence.summary)
    assert summary["ok"] is False
    assert summary["exit_code"] == 2
    assert summary["error_type"] == "nonzero_exit"
    assert summary["error"] == "command failed"
    assert summary["log_path"]


# 4. 完整结果仍然进入对象存储：摘要变短不影响按范围读取
def test_full_payload_still_available_for_range_reads(db_session, output_dir) -> None:
    task, run, action = _action(db_session)
    payload = _shell_result(2000)
    evidence = EvidenceStore().capture_result(
        db_session, task_id=task.id, run_id=run.id, action=action, result=payload,
    )
    expected = len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    assert evidence.content_size == expected
    assert evidence.inline_payload is None
    assert evidence.object_key
    chunk, truncated = EvidenceStore.read(db_session, evidence, 0, 128)
    assert chunk and truncated is True


# 5. 摘要不再被上下文装配二次切断：长度与原文一致，JSON 仍可解析
def test_summary_is_not_truncated_again_by_context_assembler(db_session, output_dir) -> None:
    task, run, action = _action(db_session)
    evidence = EvidenceStore().capture_result(
        db_session, task_id=task.id, run_id=run.id, action=action, result=_shell_result(3000),
    )
    # 装配层对 evidence.summary 的切片上限是 1600 字符，摘要必须落在这个范围内。
    assert len(evidence.summary) <= 1600
    assert json.loads(evidence.summary)["stdout_meta"]["total_lines"] == 3000
    stored = (action.result_summary or {}).get("summary")
    assert stored == evidence.summary


# 6. 分页读取结果不被后台预算收口：页内容原样保留
def test_paged_read_result_is_not_trimmed(db_session, output_dir) -> None:
    # 日志就在工具输出目录里，正好覆盖「后台任务分页读取自己的日志」这条链路。
    source = output_dir / "page.txt"
    source.write_text("\n".join(f"row-{index:05d}" for index in range(500)), encoding="utf-8")
    page = handle_read_text_file(str(source), offset=0, limit=50, max_chars=1000)
    assert page["ok"] is True
    guarded = guard_tool_output("read_text_file", json.dumps(page), max_chars=1200)
    payload = json.loads(guarded.content)
    assert payload["content"] == page["content"]
    assert "inline_trimmed" not in payload