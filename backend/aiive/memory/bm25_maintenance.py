"""BM25 索引维护命令（显式、可写进 runbook 的修复入口）。

用法::

    python -m aiive.memory.bm25_maintenance status   # 只读诊断，不修改任何对象
    python -m aiive.memory.bm25_maintenance ensure   # 幂等补建 / 重建索引

为什么需要显式命令：Alembic 迁移只在升级那一刻执行一次，扩展后装、索引被误删
或索引结构被改坏时迁移不会重跑。应用启动会自动 ensure 一次（失败不阻断启动），
本命令用于运维单独执行与排障。

退出码：0 表示 BM25 就绪；1 表示不可用（输出里带修复指引）。
"""
from __future__ import annotations

import argparse
import sys
from typing import cast

from aiive.db.base import SessionLocal
from aiive.memory.bm25_search import (
    Bm25Status,
    bm25_diagnostics,
    ensure_memory_bm25_index,
    remediation_hint,
)


def _print_diagnostics(info: dict[str, str]) -> None:
    for key in (
        "status",
        "dialect",
        "shared_preload_libraries",
        "extension_installed",
        "index_definition",
    ):
        if key in info:
            print(f"{key}: {info[key]}")


def _status() -> int:
    db = SessionLocal()
    try:
        info = bm25_diagnostics(db)
    finally:
        db.close()
    _print_diagnostics(info)
    status = cast("Bm25Status", info.get("status", "error"))
    if status != "ready":
        print(f"修复指引: {remediation_hint(status)}")
        return 1
    return 0


def _ensure(rebuild_incompatible: bool) -> int:
    db = SessionLocal()
    try:
        status = ensure_memory_bm25_index(
            db, rebuild_incompatible=rebuild_incompatible,
        )
        db.commit()
        info = bm25_diagnostics(db)
    finally:
        db.close()
    print(f"ensure 结果: {status}")
    _print_diagnostics(info)
    if status != "ready":
        print(f"修复指引: {remediation_hint(status)}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aiive.memory.bm25_maintenance",
        description="AIive 记忆 BM25（ParadeDB pg_search）索引诊断与修复",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="只读诊断 BM25 运行环境")
    ensure_parser = subparsers.add_parser("ensure", help="幂等补建 / 重建 BM25 索引")
    ensure_parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="索引结构不兼容时只报告，不删除重建",
    )
    args = parser.parse_args(argv)
    if args.command == "status":
        return _status()
    return _ensure(not args.no_rebuild)


if __name__ == "__main__":
    sys.exit(main())
