"""
Phase 6B 数据库物理空间回收（R.1 ~ R.3）。

PostgreSQL: 独立 AUTOCOMMIT connection + advisory lock + autovacuum 默认依赖
SQLite: WAL checkpoint 与 VACUUM 分开，VACUUM 仅在阈值满足时执行
均在独立低优先级任务中运行，不在用户请求事务内执行。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validated_table_name(table_name: str) -> str:
    """仅允许单段 SQL 标识符，禁止把任意输入拼入维护语句。"""
    if not _SQL_IDENTIFIER.fullmatch(table_name):
        raise ValueError("invalid_table_name")
    return table_name


class VacuumExecutor:
    """独立数据库空间回收执行器。"""

    def __init__(self, database_url: str) -> None:
        self._database_url: str = database_url

    # ------------------------------------------------------------------
    # PostgreSQL
    # ------------------------------------------------------------------

    def pg_vacuum_analyze(self, table_name: str) -> bool:
        """PostgreSQL VACUUM ANALYZE（独立 AUTOCOMMIT connection）。"""
        table_name = _validated_table_name(table_name)
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
        finally:
            engine.dispose()

    def pg_reindex(self, table_name: str, concurrently: bool = True) -> bool:
        """PostgreSQL REINDEX（优先 CONCURRENTLY）。"""
        table_name = _validated_table_name(table_name)
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
        finally:
            engine.dispose()

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
        finally:
            engine.dispose()

    def sqlite_vacuum(self, min_freelist_pct: float = 10.0, min_file_size_mb: int = 100) -> bool:
        """SQLite VACUUM（阈值检查后执行）。

        条件：
        - freelist_count / page_count > min_freelist_pct
        - 文件大小 > min_file_size_mb
        """
        if min_freelist_pct < 0 or min_file_size_mb < 0:
            raise ValueError("vacuum thresholds must be non-negative")
        engine = create_engine(self._database_url, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as conn:
                result = conn.execute(text("PRAGMA freelist_count"))
                freelist = result.scalar() or 0
                result = conn.execute(text("PRAGMA page_count"))
                page_count = result.scalar() or 0
                result = conn.execute(text("PRAGMA page_size"))
                page_size = result.scalar() or 0

                if page_count == 0:
                    return False
                file_size_mb = (page_count * page_size) / (1024 * 1024)
                if file_size_mb < min_file_size_mb:
                    logger.debug(
                        "SQLite 文件 %.1f MiB < %d MiB，跳过 VACUUM",
                        file_size_mb,
                        min_file_size_mb,
                    )
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
        finally:
            engine.dispose()
