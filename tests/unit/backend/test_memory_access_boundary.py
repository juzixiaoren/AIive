"""Incremental ORM boundary guard. Runtime regressions cover serialized data too."""
import ast
import json
from pathlib import Path


def direct_memory_reads(source):
    tree = ast.parse(source)
    aliases = {"MemoryRecord"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(n.asname or n.name for n in node.names if n.name == "MemoryRecord")
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Attribute) and n.func.attr in {"query", "get"})
                 or isinstance(n.func, ast.Name) and n.func.id == "select")
            and any((isinstance(arg, ast.Name) and arg.id in aliases)
                    or (isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name) and arg.value.id in aliases)
                    for arg in n.args)]


def test_detector_covers_query_get_select_and_alias():
    assert len(direct_memory_reads("from aiive.db.models import MemoryRecord as MR\ndb.query(MR)\ndb.get(MR, key)\nselect(MR)")) == 3


def test_runtime_identity_has_no_raw_orm_reads():
    root = Path(__file__).resolve().parents[3]
    assert not direct_memory_reads((root / "backend/aiive/memory/memory_read_model.py").read_text(encoding="utf-8"))


def test_new_raw_memory_readers_require_boundary_review():
    root = Path(__file__).resolve().parents[3]
    baseline = json.loads((Path(__file__).with_name("memory_read_boundary_baseline.json")).read_text(encoding="utf-8"))
    for path in (root / "backend/aiive").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        calls = direct_memory_reads(path.read_text(encoding="utf-8-sig"))
        assert len(calls) <= baseline.get(relative, 0), (
            f"{relative}: new raw Memory reader; use a safe boundary or document its purpose and add runtime regression"
        )
