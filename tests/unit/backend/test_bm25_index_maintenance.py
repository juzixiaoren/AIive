"""BM25 索引维护与旧数据卷预加载脚本的离线测试。

这里覆盖不需要真实 PostgreSQL 的部分：
* 非 PostgreSQL 方言下 ensure/detect 必须显式返回 unsupported_dialect；
* 索引结构判定（访问方法 + 被索引表达式）；
* 修复指引覆盖所有不可用状态；
* deploy/pg/ensure-preload.sh 对旧 PGDATA 配置的幂等补齐（真实执行脚本）；
* Dockerfile 确实把入口脚本接进了镜像。

真实 PostgreSQL + pg_search 下的补建 / 重建 / 扩展后装见
tests/integration/pg/test_pg_bm25_recall.py。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from aiive.memory.bm25_search import (
    REMEDIATION_HINTS,
    _index_is_compatible,
    detect_bm25_status,
    ensure_memory_bm25_index,
    remediation_hint,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PRELOAD_SCRIPT = REPO_ROOT / "deploy" / "pg" / "ensure-preload.sh"
DOCKERFILE = REPO_ROOT / "deploy" / "pg" / "Dockerfile"

# 真实 PostgreSQL 上 pg_get_indexdef 的输出（表达式会被加括号）。
EXPECTED_DEF = (
    "CREATE INDEX ix_memory_records_bm25 ON public.memory_records USING bm25 "
    "(id, ((content)::pdb.jieba), ((canonical_key)::pdb.literal_normalized), "
    "(((keywords)::text)::pdb.jieba))"
)


def _find_bash() -> str | None:
    """定位一个能执行 POSIX 脚本的 bash。

    Windows 上不能直接用 "bash"：CreateProcess 会先命中 System32 下的 WSL
    bash，而 WSL 访问不到 Windows 路径（且本地没有发行版时直接报错）。因此
    优先取 Git for Windows 自带的 bash。
    """
    if os.name != "nt":
        return shutil.which("bash")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for candidate in (
        Path(program_files) / "Git" / "bin" / "bash.exe",
        Path(program_files) / "Git" / "usr" / "bin" / "bash.exe",
    ):
        if candidate.exists():
            return str(candidate)
    which = shutil.which("bash")
    if which and "system32" not in which.lower():
        return which
    return None


BASH = _find_bash()
requires_bash = pytest.mark.skipif(BASH is None, reason="需要 POSIX bash 才能执行部署脚本")


# ---------------------------------------------------------------------------
# 方言与非 PostgreSQL 环境
# ---------------------------------------------------------------------------

def test_ensure_on_non_postgres_is_noop(db_session):
    """SQLite 等开发库：ensure 不建任何对象，只返回 unsupported_dialect。"""
    assert ensure_memory_bm25_index(db_session) == "unsupported_dialect"
    assert detect_bm25_status(db_session) == "unsupported_dialect"


# ---------------------------------------------------------------------------
# 索引结构判定
# ---------------------------------------------------------------------------

def test_index_definition_accepts_expected_bm25_index():
    assert _index_is_compatible(EXPECTED_DEF) is True


def test_index_definition_rejects_plain_btree_index():
    assert _index_is_compatible(
        "CREATE INDEX ix_memory_records_bm25 ON public.memory_records USING btree (content)"
    ) is False


def test_index_definition_rejects_bm25_index_missing_tokenizer():
    """访问方法是 bm25 但被索引表达式不对（例如漏了 canonical_key）同样要重建。"""
    assert _index_is_compatible(
        "CREATE INDEX ix_memory_records_bm25 ON public.memory_records USING bm25 "
        "(id, ((content)::pdb.jieba))"
    ) is False


def test_index_definition_is_case_and_whitespace_insensitive():
    assert _index_is_compatible(EXPECTED_DEF.upper().replace(" ", "  ")) is True


# ---------------------------------------------------------------------------
# 修复指引
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status",
    ["extension_missing", "index_missing", "index_incompatible", "error", "unsupported_dialect"],
)
def test_remediation_hint_covers_every_failure_state(status):
    assert REMEDIATION_HINTS[status]
    assert remediation_hint(status) == REMEDIATION_HINTS[status]


def test_remediation_hint_is_empty_when_ready():
    assert remediation_hint("ready") == ""


# ---------------------------------------------------------------------------
# 旧数据卷的 shared_preload_libraries 补齐脚本
# ---------------------------------------------------------------------------

def _run_preload(conf: Path) -> subprocess.CompletedProcess[str]:
    # as_posix：MSYS bash 不认识带反斜杠的 Windows 路径。
    return subprocess.run(
        [str(BASH), PRELOAD_SCRIPT.as_posix(), conf.as_posix()],
        capture_output=True, check=False, timeout=60,
        encoding="utf-8", errors="replace",
    )


def _write_conf(tmp_path: Path, body: str) -> Path:
    conf = tmp_path / "postgresql.conf"
    conf.write_text(body, encoding="utf-8")
    return conf


@requires_bash
def test_preload_script_appends_when_directive_absent(tmp_path: Path):
    conf = _write_conf(tmp_path, "# 旧数据卷的配置\nmax_connections = 100\n")

    result = _run_preload(conf)

    assert result.returncode == 0, result.stderr
    text = conf.read_text(encoding="utf-8")
    assert "shared_preload_libraries = 'pg_search'" in text
    assert "max_connections = 100" in text  # 原有配置保留


@requires_bash
def test_preload_script_is_idempotent(tmp_path: Path):
    conf = _write_conf(tmp_path, "max_connections = 100\n")

    assert _run_preload(conf).returncode == 0
    first = conf.read_text(encoding="utf-8")
    assert _run_preload(conf).returncode == 0

    assert conf.read_text(encoding="utf-8") == first
    assert first.count("shared_preload_libraries") == 1


@requires_bash
def test_preload_script_keeps_existing_libraries(tmp_path: Path):
    conf = _write_conf(
        tmp_path, "shared_preload_libraries = 'pg_stat_statements'\n",
    )

    assert _run_preload(conf).returncode == 0

    assert (
        "shared_preload_libraries = 'pg_stat_statements,pg_search'"
        in conf.read_text(encoding="utf-8")
    )


@requires_bash
def test_preload_script_fills_empty_value(tmp_path: Path):
    conf = _write_conf(tmp_path, "shared_preload_libraries = ''\n")

    assert _run_preload(conf).returncode == 0

    assert "shared_preload_libraries = 'pg_search'" in conf.read_text(encoding="utf-8")


@requires_bash
def test_preload_script_tolerates_missing_conf(tmp_path: Path):
    """全新数据卷：配置文件还不存在，脚本必须安静退出（initdb 会生成带预加载的配置）。"""
    result = _run_preload(tmp_path / "postgresql.conf")

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "postgresql.conf").exists()


def test_dockerfile_wires_preload_entrypoint():
    """镜像必须把补齐脚本接进入口，否则旧数据卷升级后 pg_search 仍不可用。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY deploy/pg/ensure-preload.sh /usr/local/bin/ensure-preload.sh" in text
    assert 'ENTRYPOINT ["/usr/local/bin/docker-entrypoint-bm25.sh"]' in text
