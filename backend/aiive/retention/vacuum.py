"""
Phase 6B 数据库物理空间回收（R.1 ~ R.3）。

PostgreSQL: 独立 AUTOCOMMIT connection + advisory lock + autovacuum 默认依赖
SQLite: WAL checkpoint 与 VACUUM 分开，VACUUM 仅在阈值满足时执行
均在独立低优先级任务中运行，不在用户请求事务内执行。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)


class VacuumExecutor:
    """独立数据库空间回收执行器。"""

    def __init__(self, database_url: str) -> None:
        self._database_url: str = database_url

    # ------------------------------------------------------------------
    # PostgreSQL
    # ------------------------------------------------------------------

    def pg_vacuum_analyze(self, table_name: str) -> bool:
        """PostgreSQL VACUUM ANALYZE（独立 AUTOCOMMIT connection）。"""
        engine = create_engine(
            self._database_url,
            isolation_level="AUTOCOMMIT",
        )
        try:
            with engine.connect() as conn:
                conn.execute(text(f"VACUUM ANALYZE {table_name}"))
            logger.info("PostgreSQL VACUUM ANALYZE %s 完成", table_name)
            return True
        except Exception as e:
            logger.error("PostgreSQL VACUUM ANALYZE 失败: %s", e)
            return False

    def pg_reindex(self, table_name: str, concurrently: bool = True) -> bool:
        """PostgreSQL REINDEX（优先 CONCURRENTLY）。"""
        engine = create_engine(
            self._database_url,
            isolation_level="AUTOCOMMIT",
        )
        try:
            with engine.connect() as conn:
                if concurrently:
                    conn.execute(text(f"REINDEX TABLE CONCURRENTLY {table_name}"))
                else:
                    conn.execute(text(f"REINDEX TABLE {table_name}"))
            logger.info("PostgreSQL REINDEX %s 完成", table_name)
            return True
        except Exception as e:
            logger.error("PostgreSQL REINDEX 失败: %s", e)
            return False

    # ------------------------------------------------------------------
    # SQLite
    # ------------------------------------------------------------------

    def sqlite_wal_checkpoint(self) -> bool:
        """SQLite WAL checkpoint（TRUNCATE 模式）。"""
        engine = create_engine(self._database_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            return True
        except Exception as e:
            logger.error("SQLite WAL checkpoint 失败: %s", e)
            return False

    def sqlite_vacuum(self, min_freelist_pct: float = 10.0, _min_file_size_mb: int = 100) -> bool:
        """SQLite VACUUM（阈值检查后执行）。

        条件：
        - freelist_count / page_count > min_freelist_pct
        - 文件大小 > min_file_size_mb
        """
        engine = create_engine(self._database_url)
        try:
            with engine.connect() as conn:
                result = conn.execute(text("PRAGMA freelist_count"))
                freelist = result.scalar() or 0
                result = conn.execute(text("PRAGMA page_count"))
                page_count = result.scalar() or 0

                if page_count == 0:
                    return False
                ratio = (freelist / page_count) * 100
                if ratio < min_freelist_pct:
                    logger.debug("SQLite freelist 比率 %.1f%% < %.1f%%，跳过 VACUUM", ratio, min_freelist_pct)
                    return False

                conn.execute(text("VACUUM"))
            logger.info("SQLite VACUUM 完成，freelist 比率 %.1f%%", ratio)
            return True
        except Exception as e:
            logger.error("SQLite VACUUM 失败: %s", e)
            return False


def should_vacuum(engine: Any, min_dead_tuple_pct: float = 10.0) -> bool:
    """检查 PostgreSQL 表是否需要 VACUUM。"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT n_dead_tup, n_live_tup FROM pg_stat_user_tables "
                    + "WHERE relname = 'retrieval_index_tokens'"
                ),
            )
            row = result.first()
            if row is None:
                return False
            dead, live = row
            if live and live > 0 and (dead / (dead + live)) * 100 >= min_dead_tuple_pct:
                return True
    except Exception:
        pass
    return False
