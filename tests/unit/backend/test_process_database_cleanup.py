"""Verify process-exit cleanup with real subprocesses and retained SQLite sessions."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("test_fails", [False, True])
def test_process_database_is_removed_even_with_retained_session(tmp_path, test_fails):
    script = tmp_path / "test_child.py"
    script.write_text(
        "import builtins\n"
        "from sqlalchemy import text\n"
        "from sqlalchemy.orm import Session\n"
        "from aiive.db.base import engine\n"
        "def test_retained_connection():\n"
        "    builtins.cleanup_retained_session = Session(engine)\n"
        "    builtins.cleanup_retained_session.execute(text('SELECT 1'))\n"
        f"    assert {not test_fails}\n",
        encoding="utf-8",
    )
    result, report = _run_child(script, tmp_path)
    assert result.returncode == (1 if test_fails else 0), result.stdout
    assert report["status"] == "cleaned", report
    assert not Path(report["process_database"]).exists()


def test_cleanup_failure_sets_nonzero_exit_and_writes_report(tmp_path):
    script = tmp_path / "test_child.py"
    script.write_text(
        "import conftest\n"
        "def test_injected_cleanup_failure():\n"
        "    def denied():\n"
        "        raise PermissionError('injected cleanup denial')\n"
        "    conftest._cleanup_test_db = denied\n",
        encoding="utf-8",
    )
    result, report = _run_child(script, tmp_path)
    assert result.returncode == 1, result.stdout
    assert report["status"] == "failed"
    assert "PermissionError" in report["error"]
    assert "Process database cleanup failed" in result.stdout
    # The original atexit callback still cleans the owned test DB after the
    # failure has been reported; the failed pytest status must remain nonzero.
    assert not Path(report["process_database"]).exists()


def _run_child(script, directory):
    env = dict(os.environ, PYTHONUTF8="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    args = [sys.executable, "-m", "pytest", "-p", "conftest", "-c", str(ROOT / "pyproject.toml"),
            "--confcutdir", str(directory), str(script), "-q"]
    with subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, encoding="utf-8") as process:
        try:
            output, _ = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        result = subprocess.CompletedProcess(args, process.returncode, stdout=output)
    # Windows venv launchers may spawn a different interpreter PID.
    match = re.search(r"(aiive_test_(\d+)_[A-Za-z0-9_-]+\.db)", result.stdout)
    assert match is not None, result.stdout
    pid = match.group(2)
    database = Path(tempfile.gettempdir()) / match.group(1)
    path = ROOT / "tests/artifacts/validation/cleanup" / pid / "cleanup_report.json"
    assert path.exists(), result.stdout
    report = json.loads(path.read_text(encoding="utf-8"))
    assert Path(report["process_database"]) == database
    return result, report



def test_process_database_does_not_reuse_stale_pid_file(tmp_path):
    script = r"""
import os
import tempfile
from pathlib import Path
import sys
root = Path(sys.argv[1])
tempfile.gettempdir = lambda: str(root)
stale = root / f"aiive_test_{os.getpid()}.db"
stale.write_bytes(b"old schema from an interrupted run")
import conftest
fresh = Path(conftest._TEST_DB)
assert fresh != stale
assert fresh.exists() and fresh.stat().st_size == 0
assert stale.read_bytes() == b"old schema from an interrupted run"
conftest._cleanup_test_db()
assert not fresh.exists()
assert stale.exists()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
