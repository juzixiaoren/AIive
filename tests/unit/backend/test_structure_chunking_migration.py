"""结构切分迁移契约：空库升级、存量库补列与降级回滚。

对应迁移 b8d2e6f4a1c7（Revises c3f7b2a9d1e4）：全部新增列必须带 server_default，
已有数据库升级后不丢正文与向量，存量分块按「版本为空」被维护任务识别为待重建。
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from aiive.config import settings

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PARENT_REVISION = "c3f7b2a9d1e4"
_REVISION = "b8d2e6f4a1c7"

_CHUNK_STRUCTURE_COLUMNS = {
    "heading_path", "section_id", "block_type", "source_type",
    "page_start", "page_end", "parser_version", "chunker_version",
    "embedding_input_hash",
}


def _migration_config(monkeypatch, tmp_path) -> tuple[Config, str]:
    database_url = f"sqlite:///{tmp_path / 'structure.db'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    return Config(str(_PROJECT_ROOT / "alembic.ini")), database_url


def _insert_legacy_row(engine) -> None:
    """写入一条「迁移前」形态的文档与分块（不含新增列）。"""
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO documents ("
            "id, source_path, content_hash, doc_type, status, mime_type, "
            "created_at, updated_at) VALUES ("
            "'doc-legacy', '/kb/legacy.md', 'hash-legacy', 'markdown', 'indexed', "
            "'text/markdown', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
        conn.execute(text(
            "INSERT INTO chunks ("
            "id, document_id, content, line_start, line_end, chunk_index, "
            "token_estimate, created_at) VALUES ("
            "'chunk-legacy', 'doc-legacy', '历史分块正文', 0, 3, 0, 8, "
            "'2026-01-01 00:00:00')"
        ))


def test_upgrade_head_adds_structure_columns_with_defaults(monkeypatch, tmp_path):
    config, database_url = _migration_config(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        chunk_columns = {column["name"]: column for column in inspector.get_columns("chunks")}
        document_columns = {
            column["name"] for column in inspector.get_columns("documents")
        }
        vector_columns = {
            column["name"] for column in inspector.get_columns("knowledge_chunk_vectors")
        }

        assert _CHUNK_STRUCTURE_COLUMNS <= set(chunk_columns)
        assert chunk_columns["heading_path"]["nullable"] is False
        assert str(chunk_columns["heading_path"]["default"]).strip("'\"") == ""
        assert str(chunk_columns["block_type"]["default"]).strip("'\"") == "text"
        assert chunk_columns["page_start"]["nullable"] is True
        assert {"parser_version", "chunker_version"} <= document_columns
        assert "input_version" in vector_columns
        assert "ix_chunks_embedding_input_hash" in {
            index["name"] for index in inspector.get_indexes("chunks")
        }
    finally:
        engine.dispose()


def test_existing_rows_survive_upgrade_with_default_structure(monkeypatch, tmp_path):
    config, database_url = _migration_config(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        _insert_legacy_row(engine)

        # 回滚到父修订：只删新增列，正文与向量保留
        command.downgrade(config, _PARENT_REVISION)
        downgraded = {
            column["name"] for column in inspect(engine).get_columns("chunks")
        }
        assert not (_CHUNK_STRUCTURE_COLUMNS & downgraded)

        command.upgrade(config, "head")
        with engine.connect() as conn:
            chunk = conn.execute(text(
                "SELECT content, heading_path, block_type, source_type, page_start, "
                "page_end, parser_version, chunker_version, embedding_input_hash "
                "FROM chunks WHERE id = 'chunk-legacy'"
            )).one()
            document = conn.execute(text(
                "SELECT parser_version, chunker_version FROM documents WHERE id = 'doc-legacy'"
            )).one()

        assert chunk[0] == "历史分块正文"
        assert chunk[1] == "" and chunk[3] == "text"
        assert chunk[4] is None and chunk[5] is None
        assert chunk[6] == "" and chunk[7] == "" and chunk[8] == ""
        # 版本为空即「需要重建」：维护任务据此从持久原文重新解析与切分
        assert tuple(document) == ("", "")
    finally:
        engine.dispose()
