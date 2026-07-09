"""V20 MemoryProjection：记忆投影服务。

记忆系统是 AIive 的核心组件，负责 Agent 的长期记忆管理和检索。
本模块负责将数据库中的活跃记忆以不同格式（Markdown、JSON）投影输出，
支持离线查看、文件导出和外部系统集成。

输出格式：
- Markdown: 人类可读的记忆列表，适合文档和查看
- JSON: 结构化数据，适合程序处理和外部系统消费
"""
import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from aiive.db.models import MemoryRecord

logger = logging.getLogger(__name__)


class MemoryProjection:
    """记忆投影服务：将活跃记忆导出为多种格式。

    支持 Markdown 和 JSON 两种输出格式，
    可以直接返回字符串内容，也可以写入文件系统。
    """

    def __init__(self, db: Session):
        """初始化记忆投影服务。

        Args:
            db: 数据库会话。
        """
        self._db = db

    def to_markdown(self) -> str:
        """将所有活跃记忆导出为 Markdown 格式。

        包含记忆类型标签、内容和置信度信息，按更新时间降序排列。

        Returns:
            Markdown 格式的记忆列表字符串。
        """
        records = (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == "active")
            .order_by(MemoryRecord.updated_at.desc())
            .limit(100)
            .all()
        )
        lines = ["# AIive Active Memories", "", f"Generated: {len(records)} records\n"]
        for r in records:
            lines.append(f"- [{r.memory_type}] {r.content} (confidence: {r.confidence:.2f})")
        return "\n".join(lines)

    def to_json(self) -> list[dict]:
        """将所有活跃记忆导出为 JSON 可序列化的字典列表。

        包含 id、memory_type、content、confidence 和 lineage 字段。

        Returns:
            字典列表，每个字典代表一条记忆记录。
        """
        records = (
            self._db.query(MemoryRecord)
            .filter(MemoryRecord.lifecycle_state == "active")
            .order_by(MemoryRecord.updated_at.desc())
            .limit(100)
            .all()
        )
        return [
            {
                "id": r.id,
                "memory_type": r.memory_type,
                "content": r.content,
                "confidence": r.confidence,
                "lineage": r.lineage,
            }
            for r in records
        ]

    def write_projection(self, output_dir: Path) -> dict:
        """将活跃记忆以 Markdown 和 JSON 两种格式写入指定目录。

        自动创建目标目录（如不存在），生成 memories.md 和 memories.json 两个文件。

        Args:
            output_dir: 输出目录路径。

        Returns:
            包含生成的 markdown 和 json 文件路径的字典。
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        md = self.to_markdown()
        js = self.to_json()

        (output_dir / "memories.md").write_text(md)
        (output_dir / "memories.json").write_text(json.dumps(js, indent=2, ensure_ascii=False))

        return {"markdown": str(output_dir / "memories.md"), "json": str(output_dir / "memories.json")}
