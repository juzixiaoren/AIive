"""Phase 1: WorkingStateService — 有界结构化操作上下文。

确定性字段: pending_approvals, artifact_refs, verified_tool_states,
  uncommitted_side_effects, running_tool_state → 由 Turn 生命周期维护。

语义字段: current_objective, open_loops, active_constraints →
  Phase 1 仅通过 update_working_state 工具显式更新。
"""
from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import TurnRecord, WorkingState

logger = logging.getLogger(__name__)

MAX_LIST_ITEMS = 10
MAX_FIELD_TOKENS = 2000


class WorkingStateService:
    """WorkingState 的 CRUD 与生命周期管理。每次操作使用独立短事务。"""

    def get_or_create(self, db: Session, thread_id: str, epoch_id: str = "") -> WorkingState:
        ws = db.query(WorkingState).filter(WorkingState.thread_id == thread_id).first()
        if ws is None:
            ws = WorkingState(
                thread_id=thread_id,
                epoch_id=epoch_id or None,
                open_loops=[],
                active_constraints=[],
                pending_approvals=[],
                artifact_refs=[],
                verified_tool_states=[],
                uncommitted_side_effects=[],
                running_tool_state=[],
                version=1,
            )
            db.add(ws)
            db.flush()
        return ws

    # ── 确定性字段：由 Turn 生命周期维护 ──

    def add_running_tool(
        self, db: Session, thread_id: str,
        turn_record_id: str, execution_id: str,
        tool_call_id: str, name: str,
    ) -> None:
        """工具开始前：添加运行中工具记录。"""
        ws = self.get_or_create(db, thread_id)
        running = list(ws.running_tool_state or [])
        running.append({
            "turn_record_id": turn_record_id,
            "execution_id": execution_id,
            "tool_call_id": tool_call_id,
            "name": name,
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        if len(running) > MAX_LIST_ITEMS:
            running = running[-MAX_LIST_ITEMS:]
        ws.running_tool_state = running
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)

    def remove_running_tool(
        self, db: Session, thread_id: str, tool_call_id: str,
    ) -> None:
        """工具完成或失败后：移除运行中工具记录。"""
        ws = self.get_or_create(db, thread_id)
        running = list(ws.running_tool_state or [])
        running = [t for t in running if t.get("tool_call_id") != tool_call_id]
        ws.running_tool_state = running
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)

    def update_verified_tool_state(
        self, db: Session, thread_id: str, tool_name: str, state: dict[str, Any],
    ) -> None:
        """工具验证成功后：更新已验证工具状态。

        verified_tool_states 每条目保持 tool_name 去重语义，并将 tool_call_id
        提升为顶层字段（Phase 3 以 tool_call_id 作为稳定键），同时保留完整 state。
        """
        ws = self.get_or_create(db, thread_id)
        states = list(ws.verified_tool_states or [])
        states = [s for s in states if s.get("tool_name") != tool_name]
        states.append({
            "tool_name": tool_name,
            "tool_call_id": state.get("tool_call_id"),
            "state": state,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        })
        if len(states) > MAX_LIST_ITEMS:
            states = states[-MAX_LIST_ITEMS:]
        ws.verified_tool_states = states
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)

    def add_uncommitted_side_effect(
        self, db: Session, thread_id: str, ref: str, description: str, risk: str,
    ) -> None:
        """副作用执行前：添加未提交副作用记录。"""
        ws = self.get_or_create(db, thread_id)
        effects = list(ws.uncommitted_side_effects or [])
        effects.append({"ref": ref, "description": description, "risk": risk})
        if len(effects) > MAX_LIST_ITEMS:
            effects = effects[-MAX_LIST_ITEMS:]
        ws.uncommitted_side_effects = effects
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)

    def remove_uncommitted_side_effect(self, db: Session, thread_id: str, ref: str) -> None:
        """副作用提交/回滚后：移除记录。"""
        ws = self.get_or_create(db, thread_id)
        effects = list(ws.uncommitted_side_effects or [])
        effects = [e for e in effects if e.get("ref") != ref]
        ws.uncommitted_side_effects = effects
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)

    def add_artifact_ref(
        self, db: Session, thread_id: str, ref: str, kind: str, description: str,
    ) -> None:
        """Artifact 持久化时：写入 artifact 引用。"""
        ws = self.get_or_create(db, thread_id)
        refs = list(ws.artifact_refs or [])
        refs.append({"ref": ref, "kind": kind, "description": description})
        if len(refs) > MAX_LIST_ITEMS:
            refs = refs[-MAX_LIST_ITEMS:]
        ws.artifact_refs = refs
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)

    def add_pending_approval(
        self, db: Session, thread_id: str, approval_id: str, action: str,
    ) -> None:
        """审批请求时：添加待审批记录。"""
        ws = self.get_or_create(db, thread_id)
        approvals = list(ws.pending_approvals or [])
        approvals.append({
            "id": approval_id, "action": action,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        })
        if len(approvals) > MAX_LIST_ITEMS:
            approvals = approvals[-MAX_LIST_ITEMS:]
        ws.pending_approvals = approvals
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)

    def remove_pending_approval(self, db: Session, thread_id: str, approval_id: str) -> None:
        """审批处理后：移除记录。"""
        ws = self.get_or_create(db, thread_id)
        approvals = list(ws.pending_approvals or [])
        approvals = [a for a in approvals if a.get("id") != approval_id]
        ws.pending_approvals = approvals
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)

    # ── 崩溃恢复 ──

    def recover_orphaned_tools(self, db: Session, thread_id: str) -> None:
        """清理崩溃残留的孤立运行状态。

        - running_tool_state：对应 TurnRecord 已非 running 的条目移除；
        - uncommitted_side_effects：ref 对应上述被移除的孤立运行条目
          （即其 Turn 已达终态、正常清理路径未走到）的记录一并移除，
          否则崩溃残留会永久阻塞 Segment 密封（check_sealable 检查该字段）。
          正常 execution_unknown 挂起副作用（有终态 Turn 但确认流程仍在进行、
          且无对应 running 条目）不在此清理范围。
        """
        ws = self.get_or_create(db, thread_id)
        running = list(ws.running_tool_state or [])
        stale: list[str] = []
        for entry in running:
            tr = db.get(TurnRecord, entry.get("turn_record_id", ""))
            if tr is None or tr.status != "running":
                stale.append(entry.get("tool_call_id", ""))
        if stale:
            running = [t for t in running if t.get("tool_call_id") not in stale]
            ws.running_tool_state = running
            ws.version = (ws.version or 0) + 1
            ws.updated_at = datetime.now(timezone.utc)
            logger.info("已恢复 %d 个孤立运行工具 thread=%s", len(stale), thread_id)

            effects = list(ws.uncommitted_side_effects or [])
            cleaned_effects = [e for e in effects if e.get("ref") not in stale]
            if len(cleaned_effects) != len(effects):
                ws.uncommitted_side_effects = cleaned_effects
                logger.info(
                    "已清理 %d 个孤立未提交副作用 thread=%s",
                    len(effects) - len(cleaned_effects), thread_id,
                )

    # ── 语义字段：Phase 1 仅显式更新 ──

    def update_semantic_field(
        self, db: Session, thread_id: str,
        field: str, operation: str, payload: dict[str, Any],
        _source_turn_id: str, _idempotency_key: str,
    ) -> None:
        ws = self.get_or_create(db, thread_id)
        current = list(getattr(ws, field, None) or [])
        if operation == "add":
            current.append(payload)
            if len(current) > MAX_LIST_ITEMS:
                current = current[-MAX_LIST_ITEMS:]
        elif operation == "remove":
            item_id = payload.get("id", "")
            current = [it for it in current if it.get("id") != item_id]
        elif operation == "update":
            item_id = payload.get("id", "")
            for i, it in enumerate(current):
                if it.get("id") == item_id:
                    current[i] = {**it, **payload}
                    break
        setattr(ws, field, current)
        ws.version = (ws.version or 0) + 1
        ws.updated_at = datetime.now(timezone.utc)
        serialized = _json.dumps(current, ensure_ascii=False)
        ws.token_count = len(serialized.encode("utf-8")) // 2

    # ── 渲染为上下文字段 ──

    def render_for_context(self, db: Session, thread_id: str, token_budget: int) -> str:
        """将 WorkingState 渲染为有界系统消息文本块，不超过 token_budget。"""
        ws = self.get_or_create(db, thread_id)
        parts: list[str] = ["## Working State（当前操作上下文，有界）"]

        if ws.current_objective:
            objective_block = f"### 当前目标\n{ws.current_objective}"
            parts.append(objective_block)

        if ws.open_loops:
            parts.append("### 未完成循环")
            for loop in (ws.open_loops or [])[:MAX_LIST_ITEMS]:
                prio = loop.get("priority", "P?")
                desc = loop.get("description", "")
                parts.append(f"- [{prio}] {desc}")

        if ws.active_constraints:
            parts.append("### 活跃约束")
            for c in (ws.active_constraints or [])[:MAX_LIST_ITEMS]:
                parts.append(f"- {c.get('description', '')} (来源: {c.get('source', 'unknown')})")

        if ws.pending_approvals:
            parts.append("### 待审批")
            for a in (ws.pending_approvals or [])[:MAX_LIST_ITEMS]:
                parts.append(f"- {a.get('action', '')} (请求时间: {a.get('requested_at', '')})")

        if ws.artifact_refs:
            parts.append("### 引用的 Artifact")
            for a in (ws.artifact_refs or [])[:MAX_LIST_ITEMS]:
                parts.append(f"- {a.get('ref', '')}: {a.get('description', '')}")

        full_text = "\n".join(parts).rstrip() + "\n"
        # token 预算裁剪：从末尾逐段删除直到满足预算
        # 估算: UTF-8 字节数 / 2 ≈ token 数（保守）
        estimated = len(full_text.encode("utf-8")) // 2
        if estimated <= token_budget:
            return full_text

        truncated_parts = list(parts)
        while len(truncated_parts) > 1:
            truncated_parts.pop()
            candidate = "\n".join(truncated_parts).rstrip() + "\n"
            if len(candidate.encode("utf-8")) // 2 <= token_budget:
                return candidate

        return truncated_parts[0] if truncated_parts else ""
