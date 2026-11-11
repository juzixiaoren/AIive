"""共享运行时契约不可反向依赖图执行或数据库。"""
import os
from pathlib import Path
import subprocess
import sys


def test_shared_runtime_contracts_import_without_execution_stack():
    root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(root / "backend")}
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from aiive.knowledge.document_status import ACTIVE_STATUSES, INACTIVE_STATUSES
assert not ACTIVE_STATUSES & INACTIVE_STATUSES
from aiive.runtime.system_contract import build_runtime_identity
from aiive.runtime.tool_events import StreamToolCallMatcher, parse_tool_execution_receipt
assert build_runtime_identity(None) == ""
assert parse_tool_execution_receipt('{"execution_status":"running"}').status == "execution_unknown"
assert not any(name in sys.modules for name in (
    "aiive.runtime.agent_graph", "aiive.db.base", "langgraph", "langchain_openai",
    "aiive.knowledge.ingestor",
))
"""],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
