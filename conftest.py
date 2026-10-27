"""AIive 测试套件根 conftest：统一运行环境与统一结果输出。

职责（必须在导入任何 aiive 模块之前完成）：

1. 统一数据库：所有测试一律使用进程级 SQLite 文件库，消除对本地 PostgreSQL
   的外部依赖，保证任何机器、任何 CI 环境下都能以相同方式运行同一套测试。
2. 统一开关：SQLite 不具备 pgvector，故强制关闭记忆向量能力；其余 AIIVE_*
   配置仍从仓库根 .env 读取，保持与生产默认值一致。
3. 统一输出：注册结果汇总插件，失败/错误一律打印「节点 + 源码位置 + 异常
   类型 + 异常消息」，并附带最慢用例与跳过原因统计，避免不同测试文件各自
   用 print 输出导致结果无法对照。

本文件位于仓库根，对 tests/ 下全部层级（unit / integration / flow）生效。
"""
from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

# ── 1. 环境变量：先于任何 aiive 导入 ──────────────────────────────────────


def _load_dotenv() -> None:
    """从仓库根 .env 加载 AIIVE_ 前缀配置，供依赖真实配置的测试使用。

    刻意不加载 DATABASE_URL：数据库由本文件统一指定为 SQLite，避免本机
    Postgres 实例的脏数据影响测试结果。
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key.startswith("AIIVE_") and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# 文件名带 PID：同一进程内多会话共享同一物理库（backend/tests 的既有设计），
# 而并行的多个 pytest 进程互不干扰（固定文件名会导致并发 drop_all 假失败）。
_TEST_DB = os.path.join(tempfile.gettempdir(), f"aiive_test_{os.getpid()}.db")


@atexit.register
def _cleanup_test_db() -> None:
    try:
        os.remove(_TEST_DB)
    except OSError:
        pass


os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
# pgvector 为 PostgreSQL 专属能力，SQLite 下必须关闭，否则 Settings 校验直接失败
os.environ["AIIVE_MEMORY_VECTOR_ENABLED"] = "false"
# 认证默认开启，但既有用例都以匿名方式调用 API；测试层统一关闭，
# 认证行为由 tests/unit/backend/test_auth.py 显式打开 settings.aiive_auth_enabled 验证。
os.environ["AIIVE_AUTH_ENABLED"] = "false"

# ── 1.5 进程级测试库建表 ──────────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def _prepare_process_test_db() -> None:
    """为进程级 SQLite 测试库建表（每个 pytest 进程只建一次）。

    单元/流程层中刻意不请求 db_session 的用例（例如需要让生产代码自开会话
    提交的场景）会经 SessionLocal 回落到本库，因此这里必须保证表结构存在；
    集成层 conftest 会在每个用例前 drop_all/create_all，两者互不冲突。
    """
    from aiive.db.base import create_all

    create_all()

# ── 2. 统一输出插件 ──────────────────────────────────────────────────────

_SEPARATOR = "=" * 78

# nodeid -> 累计耗时（setup + call + teardown）
_DURATIONS: dict[str, float] = {}


def pytest_report_header(config) -> str:
    """在测试开始时打印统一的运行上下文，便于复现与比对结果。"""
    return (
        f"{_SEPARATOR}\n"
        f"AIive 测试套件 | rootdir: {config.rootpath}\n"
        f"数据库: {os.environ.get('DATABASE_URL', '<unset>')}\n"
        f"向量召回: {os.environ.get('AIIVE_MEMORY_VECTOR_ENABLED', 'false')} (SQLite 下强制关闭)\n"
        f"{_SEPARATOR}"
    )


def pytest_runtest_logreport(report) -> None:
    """累计每个用例耗时，供汇总输出最慢用例。"""
    duration = getattr(report, "duration", None)
    if duration is None:
        return
    _DURATIONS[report.nodeid] = _DURATIONS.get(report.nodeid, 0.0) + float(duration)


def _crash_lines(report) -> tuple[str, str]:
    """从 longrepr 中提取（源码位置, 异常摘要），兼容失败与错误两种形态。"""
    longrepr = report.longrepr
    if longrepr is None:
        return "", "<无异常信息>"
    crash = getattr(longrepr, "reprcrash", None)
    if crash is not None:
        return f"{crash.path}:{crash.lineno}", (crash.message or "").strip()
    text = str(longrepr).rstrip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    exc_line = next((ln[2:].strip() for ln in reversed(lines) if ln.startswith("E ")), "")
    return "", exc_line or (lines[-1] if lines else "<无异常信息>")


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """统一汇总：异常明细 + 跳过原因 + 最慢用例。

    所有失败/错误都以同一格式打印，保证任何测试失败都能直接看到根因，
    而不是被吞掉的 print 或无声的 except。
    """
    write = terminalreporter.write_line
    stats = terminalreporter.stats
    failed = list(stats.get("failed", []))
    errors = list(stats.get("error", []))

    if failed or errors:
        write("")
        write(_SEPARATOR)
        write(f"异常明细（{len(failed)} 失败 / {len(errors)} 错误）")
        write(_SEPARATOR)
        for idx, report in enumerate(failed + errors, start=1):
            location, message = _crash_lines(report)
            write(f"[{idx}] {report.nodeid}  (阶段: {report.when})")
            if location:
                write(f"    位置: {location}")
            write(f"    异常: {message}")
            write("")

    skipped = list(stats.get("skipped", []))
    if skipped:
        reasons: dict[str, int] = {}
        for report in skipped:
            reason = "未标注原因"
            if report.longrepr:
                tail = [ln.strip() for ln in str(report.longrepr).splitlines() if ln.strip()]
                if tail:
                    reason = tail[-1]
            reasons[reason] = reasons.get(reason, 0) + 1
        write(_SEPARATOR)
        write(f"跳过统计（{len(skipped)}）：")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            write(f"  {count:>3} x {reason}")
        write(_SEPARATOR)

    if _DURATIONS:
        slowest = sorted(_DURATIONS.items(), key=lambda kv: -kv[1])[:10]
        write(_SEPARATOR)
        write("最慢用例 Top 10：")
        for nodeid, duration in slowest:
            write(f"  {duration:>7.2f}s  {nodeid}")
        write(_SEPARATOR)
