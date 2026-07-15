"""Phase 1: ToolResultNormalizer — 大型工具结果在下一次 LLM 调用前引用化。

必须在构造 ToolMessage 之前运行，不得在 _finalize_turn 中处理。
Artifact 在首次引用前的独立短事务中持久化。
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
import uuid as _uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError

from aiive.db.base import SessionLocal
from aiive.db.models import Artifact
from aiive.runtime.token_models import TokenCount

logger = logging.getLogger(__name__)

SINGLE_TOOL_RESULT_INLINE_LIMIT = 2000  # token 上限


@dataclass
class ToolResultView:
    """规范化后的结果：内联文本或 artifact 引用。"""
    inline: str | None = None
    is_reference: bool = False
    reference: dict[str, Any] | None = None

    def to_tool_message_content(self) -> str:
        if not self.is_reference or self.inline:
            return self.inline or ""
        return _json.dumps(self.reference, ensure_ascii=False)


class ToolResultNormalizer:
    """工具结果规范化：确定性引用化大型输出。

    Phase 1 不使用 LLM 摘要。
    """

    def __init__(self, token_counter: Any, model: str):  # TokenCounter Protocol
        self._token_counter: Any = token_counter
        self._model: str = model
        self.SINGLE_RESULT_INLINE_LIMIT: int = SINGLE_TOOL_RESULT_INLINE_LIMIT

    def normalize(
        self,
        result_text: str,
        thread_id: str,
        trace_id: str,
        turn_record_id: str = "",
        execution_id: str = "",
        tool_call_id: str = "",
    ) -> ToolResultView:
        """规范化单个工具结果。

        在线内限制内 → 直接返回。
        超过限制 → 持久化 Artifact，返回确定性预览。
        Artifact 持久化失败 → 返回有界错误预览，绝不传完整大型结果。
        """
        tc: TokenCount = self._token_counter.count_messages(
            self._model, [{"role": "user", "content": result_text}],
        )
        if tc.estimated_tokens <= self.SINGLE_RESULT_INLINE_LIMIT:
            return ToolResultView(inline=result_text, is_reference=False)

        # ── 持久化 Artifact（独立短事务）──
        content_hash = hashlib.sha256(result_text.encode()).hexdigest()
        artifact_ref_str = f"artifact://{_uuid.uuid4().hex[:12]}"

        db = SessionLocal()
        try:
            artifact = Artifact(
                thread_id=thread_id,
                trace_id=trace_id,
                kind="tool_result",
                ref=artifact_ref_str,
                content=result_text,
                content_hash=content_hash,
                token_count=tc.estimated_tokens,
                turn_record_id=turn_record_id or None,
                execution_id=execution_id or None,
                tool_call_id=tool_call_id or None,
            )
            db.add(artifact)
            db.commit()
        except IntegrityError:
            # 唯一约束 (turn_record_id, tool_call_id) 冲突：重试已存在 Artifact，
            # 返回其稳定 ref，绝不重复创建。
            db.rollback()
            logger.warning("Artifact 已存在，复用既有记录 tool_call_id=%s", tool_call_id)
            existing = (
                db.query(Artifact)
                .filter(
                    Artifact.turn_record_id == (turn_record_id or None),
                    Artifact.tool_call_id == (tool_call_id or None),
                )
                .order_by(Artifact.created_at.desc())
                .first()
            )
            if existing is not None:
                preview = self._build_deterministic_preview(result_text, existing.ref)
                return ToolResultView(is_reference=True, reference=preview)
            # 极端情况：冲突但查不到（并发删除），降级为错误预览
            return ToolResultView(
                is_reference=True,
                reference={
                    "artifact_ref": f"artifact://error/{tool_call_id}",
                    "summary": "（Artifact 持久化冲突 — 结果不可用）",
                    "content_hash": content_hash,
                    "byte_count": len(result_text.encode("utf-8")),
                    "error": "persistence_conflict",
                },
            )
        except Exception:
            db.rollback()
            logger.exception("Artifact 持久化失败 tool_call_id=%s", tool_call_id)
            # 有界错误预览 — 绝不传完整大型结果给模型
            return ToolResultView(
                is_reference=True,
                reference={
                    "artifact_ref": f"artifact://error/{tool_call_id}",
                    "summary": "（Artifact 持久化失败 — 结果不可用）",
                    "content_hash": content_hash,
                    "byte_count": len(result_text.encode("utf-8")),
                    "error": "persistence_failure",
                },
            )
        finally:
            db.close()

        preview = self._build_deterministic_preview(result_text, artifact_ref_str)
        return ToolResultView(is_reference=True, reference=preview)

    def normalize_turn(
        self, turn_dicts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """规范化历史 Turn 中的 tool_results。

        用于有界历史读取：超限工具结果引用化。
        不持久化 Artifact（原 Turn 已存在）。
        """
        result = []
        for d in turn_dicts:
            if d.get("type") == "tool_result":
                raw = d.get("tool_result", {})
                result_text = _json.dumps(raw, ensure_ascii=False, default=str)
                tc = self._token_counter.count_messages(
                    self._model, [{"role": "user", "content": result_text}],
                )
                if tc.estimated_tokens > self.SINGLE_RESULT_INLINE_LIMIT:
                    existing_ref = d.get("artifact_ref", "")
                    preview = self._build_deterministic_preview(
                        result_text, existing_ref or "artifact://historical",
                    )
                    d = dict(d)
                    d["type"] = "tool_result_ref"
                    d["tool_result"] = preview
            result.append(d)
        return result

    def normalize_turn_sparse(
        self, turn_dicts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """激进引用化：所有 tool_results 全部替换为引用（无在线内检查）。"""
        result = []
        for d in turn_dicts:
            if d.get("type") == "tool_result":
                result_text = _json.dumps(d.get("tool_result", {}), ensure_ascii=False, default=str)
                existing_ref = d.get("artifact_ref", "")
                preview = self._build_deterministic_preview(
                    result_text, existing_ref or "artifact://sparse",
                )
                d = dict(d)
                d["type"] = "tool_result_ref"
                d["tool_result"] = preview
            result.append(d)
        return result

    @staticmethod
    def _build_deterministic_preview(result_text: str, artifact_ref: str) -> dict[str, Any]:
        """确定性预览规则（无 LLM）。"""
        preview: dict[str, Any] = {
            "artifact_ref": artifact_ref,
            "content_hash": hashlib.sha256(result_text.encode()).hexdigest(),
            "byte_count": len(result_text.encode("utf-8")),
        }

        # 短文本直接放
        if len(result_text) <= 500:
            preview["summary"] = result_text
        else:
            preview["summary"] = result_text[:200] + "\n...[已截断]...\n" + result_text[-200:]

        # JSON 结构化信息提取
        try:
            data = _json.loads(result_text)
            if isinstance(data, dict):
                preview["top_keys"] = list(data.keys())[:20]
                for k, v in data.items():
                    if isinstance(v, list):
                        preview[f"len_{k}"] = len(v)
                    elif isinstance(v, dict):
                        preview[f"keys_{k}"] = list(v.keys())[:10]
            elif isinstance(data, list):
                preview["item_count"] = len(data)
                if data and isinstance(data[0], dict):
                    preview["columns"] = list(data[0].keys())
        except (_json.JSONDecodeError, TypeError):
            pass

        # 错误模式检测
        lines = result_text.split("\n")
        errors = [l for l in lines if any(
            kw in l.lower() for kw in ("error", "exception", "traceback", "fail")
        )]
        if errors:
            preview["error_count"] = len(errors)
            preview["first_error"] = errors[0][:200]

        # 表格检测（CSV/TSV）
        has_tabs = any("\t" in l for l in lines[:5]) if lines else False
        has_commas = any("," in l for l in lines[:5]) if lines else False
        if (has_tabs or has_commas) and len(lines) > 1:
            preview["row_count"] = len(lines)
            if lines:
                preview["header"] = lines[0][:200]

        return preview
