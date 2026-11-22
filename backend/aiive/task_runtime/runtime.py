"""Persistent Task Runtime：每次 wake 创建 AgentRun，并推进有界 Action 循环。"""
from __future__ import annotations

import logging
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.control.approval_service import TaskApprovalService
from aiive.control.capability_broker import CapabilityBroker
from aiive.db.models import AgentAction, AgentRun, AgentTask, TaskArtifact, TaskEvidence
from aiive.task_runtime.agent_executor import TaskAgentExecutor
from aiive.task_runtime.errors import AgentOutputValidationError, TaskModelBudgetError
from aiive.tools.tool_validation import validate_tool_params
from aiive.core.function_calling import correction_allowed, CallValidation, output_error
from aiive.task_runtime.evidence import EvidenceStore
from aiive.task_runtime.goal_service import GoalService
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import (
    ActionProposal,
    ActionStatus,
    RunStatus,
    TaskBudget,
    TaskReport,
    TaskStatus,
)
from aiive.task_runtime.tool_injection import (
    WORKER_RESIDENT_CAPABILITIES,
    discovery_state,
    merge_state,
    record_definition,
    record_search,
)
from aiive.tools.base import outcome_is_unknown

logger = logging.getLogger(__name__)


def action_outcome(result: dict[str, Any]) -> str:
    """把工具执行 payload 映射为 Action 终态语义。

    成功 / 确定性失败 / 已确认取消 / 外部副作用未知必须互斥，未确认的结果
    一律优先判为 unknown，不能被降级成成功或普通失败。
    """
    if result.get("ok"):
        return "succeeded"
    if outcome_is_unknown(result):
        return "unknown"
    error_type = str(result.get("error_type") or "")
    if error_type == "cancelled":
        return "cancelled"
    if error_type == "precondition_failed":
        return "invalidated"
    return "failed"


class PersistentTaskRuntime:
    def __init__(
        self,
        db: Session,
        *,
        agent_executor: TaskAgentExecutor | None = None,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self.db: Session = db
        self.repo: TaskRepository = TaskRepository(db)
        self.agent: TaskAgentExecutor = agent_executor or TaskAgentExecutor(db)
        self.evidence: EvidenceStore = evidence_store or EvidenceStore()
        self.active_run_id: str | None = None

    def run(self, task_id: str, *, trigger: str = "dispatch") -> dict[str, Any]:
        candidate = self.repo.get_task(task_id, for_update=True)
        pending_report = None
        initial_budget_issue = None
        if candidate is not None and candidate.status == TaskStatus.QUEUED.value:
            budget = TaskBudget.from_stored(candidate.budgets)
            pending_report = (candidate.task_state or {}).get("pending_report")
            if budget.max_runs is not None and int((candidate.usage or {}).get("runs", 0)) >= budget.max_runs:
                initial_budget_issue = "run_budget_exhausted"
            else:
                initial_budget_issue = self._budget_issue(
                    candidate, budget, before_model=self.repo.ready_action(candidate.id) is None,
                )
        report_only = pending_report is not None or initial_budget_issue is not None
        task, run = self.repo.start_run(
            task_id, trigger="report" if report_only else trigger, count_toward_budget=not report_only,
        )
        self.active_run_id = run.id
        self.db.commit()
        if pending_report is not None:
            return self._finish_report(task, run, pending_report)
        if initial_budget_issue:
            return self._stop_for_budget(task, run, initial_budget_issue)
        self._ensure_isolation(task, run)
        self.db.commit()
        budget = TaskBudget.from_stored(task.budgets)

        goals = GoalService(self.db) if GoalService.is_goal_task(task) else None
        if goals is not None and not goals.on_run_started(task, run):
            # 没有可执行的步骤（全部在等待/已完成）：不消耗 LLM，直接回到等待态。
            self.repo.finish_run(run, summary={"blocked": "goal_waiting"})
            self.repo.transition_task(
                task, TaskStatus.BLOCKED_USER.value,
                event_type="goal_waiting",
                event_payload={"reason": "no_runnable_step"},
                visibility="conversation",
            )
            self.repo.checkpoint(task.id, reason="goal_waiting")
            self.db.commit()
            return {"status": task.status, "task_id": task.id, "goal": "waiting"}
        self.db.commit()

        correction_failures = int((task.task_state or {}).get("validation_failures", 0))
        completed_this_run = 0
        last_model: str | None = None
        run_tokens: dict[str, int] = {}
        while completed_this_run < (budget.max_actions_per_run or 8):
            from aiive.worker.outbox_execution_auth import require_execution_authorization
            from aiive.worker.job_source_guard import task_sources_visible

            require_execution_authorization()
            if not task_sources_visible(self.db, task.id):
                return self._complete(
                    task, run, status="partial", summary="任务来源已被遗忘，停止执行。",
                    unresolved=["forget_source_blocked"],
                )
            self.db.refresh(task)
            if goals is not None and goals.is_paused(task):
                # 暂停是用户显式意图：停止本轮推理，保留已完成步骤与证据。
                self.repo.finish_run(run, status=RunStatus.INTERRUPTED.value, error="goal_paused")
                self.repo.transition_task(
                    task, TaskStatus.BLOCKED_USER.value,
                    event_type="goal_paused", event_payload={}, visibility="conversation",
                )
                self.repo.checkpoint(task.id, reason="goal_paused")
                self.db.commit()
                return {"status": task.status, "task_id": task.id, "goal": "paused"}
            if task.status == TaskStatus.CANCELLED.value:
                self.repo.finish_run(run, status=RunStatus.INTERRUPTED.value, error="task_cancelled")
                self.db.commit()
                return {"status": task.status, "task_id": task.id}
            ready = self.repo.ready_action(task.id)
            budget_issue = self._budget_issue(task, budget, before_model=ready is None)
            if budget_issue:
                return self._stop_for_budget(task, run, budget_issue)
            if ready is not None:
                outcome = self._dispatch_ready(task, run, ready)
                completed_this_run += 1
                if outcome in {"blocked", "terminal"}:
                    return {"status": task.status, "task_id": task.id, "action_id": ready.id}
                continue

            broker = CapabilityBroker(self.db)
            available = broker.available_registry(task)
            catalog = available.catalog()
            entries = {item["capability_id"]: item for item in catalog}
            resident = [entries[key] for key in WORKER_RESIDENT_CAPABILITIES if key in entries]
            if not catalog:
                self.repo.transition_task(
                    task, TaskStatus.BLOCKED_NODE.value,
                    event_type="task_blocked_node",
                    event_payload={"reason": "no_scoped_capabilities_or_node_offline"},
                    visibility="conversation",
                )
                self.repo.finish_run(run, summary={"blocked": "node"})
                self.repo.checkpoint(task.id, reason="blocked_node")
                self.db.commit()
                return {"status": task.status, "task_id": task.id}

            decision = None
            deterministic = self._deterministic_proposal(task)
            if deterministic is not None:
                proposal = deterministic
                state = dict(task.task_state or {})
                state["deterministic_action_proposed"] = True
                task.task_state = state
                run.input_snapshot = {
                    "task_id": task.id,
                    "executor_type": "deterministic",
                    "resident_capability_ids": [item["capability_id"] for item in resident],
                    "catalog_size": len(catalog),
                    "prompt_refs": [],
                }
            elif task.executor_type == "deterministic":
                latest = self.db.query(AgentAction).filter(
                    AgentAction.task_id == task.id,
                ).order_by(AgentAction.sequence.desc()).first()
                if latest is not None and latest.status == ActionStatus.SUCCEEDED.value:
                    return self._complete(
                        task, run, status="succeeded",
                        summary="确定性任务已执行并保存 Evidence。",
                    )
                return self._complete(
                    task, run, status="partial",
                    summary="确定性 Action 未能完成。",
                    unresolved=[
                        (latest.error_message or f"deterministic_action_{latest.status}")
                        if latest else "deterministic_action_missing"
                    ],
                )
            else:
                usage = dict(task.usage or {})
                usage["model_calls"] = int(usage.get("model_calls", 0)) + 1
                task.usage = usage
                self.repo.renew_run(run, lease_seconds=180)
                self.db.commit()
                try:
                    decision = self.agent.decide(task, resident)
                except TaskModelBudgetError as error:
                    task.usage = {**(task.usage or {}), "model_calls": max(0, usage["model_calls"] - 1)}
                    return self._stop_for_budget(task, run, str(error))
                except AgentOutputValidationError as error:
                    last_model = error.model
                    run_tokens = self._merge_usage(run_tokens, error.token_usage)
                    self.repo.renew_run(run, lease_seconds=180)
                    self._record_tokens(task, error.token_usage)
                    run.token_usage = run_tokens
                    run.model = last_model
                    correction_failures += 1
                    budget_issue = self._budget_issue(task, budget, before_model=True)
                    if budget_issue:
                        return self._stop_for_budget(task, run, budget_issue)
                    if not correction_allowed(correction_failures):
                        return self._validation_failed(task, run, run_tokens, last_model)
                    task.task_state = {**(task.task_state or {}), "validation_feedback": error.payload(), "validation_failures": correction_failures, "validation_messages": error.feedback}
                    self.db.commit()
                    continue
                self.repo.renew_run(run, lease_seconds=180)
                last_model = decision.model
                run_tokens = self._merge_usage(run_tokens, decision.token_usage)
                self._record_tokens(task, decision.token_usage)
                run.token_usage = run_tokens
                run.model = last_model
                self.db.commit()
                run.input_snapshot = {
                    **decision.input_snapshot,
                    "prompt_refs": decision.prompt_refs,
                }
                proposal = decision.proposal
                if proposal.progress_draft.strip():
                    task.task_state = {**(task.task_state or {}), "progress_draft": proposal.progress_draft}
                budget_issue = self._budget_issue(task, budget, before_model=False)
                if budget_issue:
                    return self._stop_for_budget(task, run, budget_issue)
            if proposal.decision == "action":
                registration = available.get(proposal.capability_id)
                if registration is not None:
                    validation = validate_tool_params(registration, proposal.arguments)
                    if not validation.ok:
                        correction_failures += 1
                        budget_issue = self._budget_issue(task, budget, before_model=True)
                        if budget_issue:
                            return self._stop_for_budget(task, run, budget_issue)
                        if task.executor_type == "deterministic" or not correction_allowed(correction_failures):
                            return self._validation_failed(task, run, run_tokens, last_model)
                        payload = validation.error_payload(proposal.capability_id)
                        from langchain_core.messages import HumanMessage, message_to_dict
                        repair = [message_to_dict(HumanMessage(content=json.dumps(payload, ensure_ascii=False)))]
                        if decision is not None and decision.call_message is not None:
                            call_id = str(decision.call_message.tool_calls[0]["id"])
                            repair = CallValidation({call_id: payload}).retry_turn(decision.call_message)
                        task.task_state = {**(task.task_state or {}), "validation_feedback": payload,
                                           "validation_failures": correction_failures, "validation_messages": repair}
                        self.db.commit()
                        continue
                    proposal.arguments = validation.validated_params
            if proposal.decision == "watch":
                from aiive.task_runtime.watchers import WatcherService

                try:
                    WatcherService(self.db).create_from_proposal(task, proposal.watch)
                except (ValueError, OSError, RuntimeError) as error:
                    correction_failures += 1
                    budget_issue = self._budget_issue(task, budget, before_model=True)
                    if budget_issue:
                        return self._stop_for_budget(task, run, budget_issue)
                    if not correction_allowed(correction_failures):
                        return self._complete(
                            task, run, status="partial", summary="无法建立任务等待条件。",
                            unresolved=[str(error)[:500]], model=last_model, token_usage=run_tokens,
                        )
                    payload = output_error("invalid_watch", path="watch", expected="valid watch and authorized target")
                    payload["issues"][0]["message"] = str(error)[:500]
                    from langchain_core.messages import HumanMessage, message_to_dict
                    repair = [message_to_dict(HumanMessage(content=json.dumps(payload, ensure_ascii=False)))]
                    if decision is not None and decision.call_message is not None:
                        call_id = str(decision.call_message.tool_calls[0]["id"])
                        repair = CallValidation({call_id: payload}).retry_turn(decision.call_message)
                    task.task_state = {**(task.task_state or {}), "validation_feedback": payload,
                                       "validation_failures": correction_failures, "validation_messages": repair}
                    self.db.commit()
                    continue
            correction_failures = 0
            state = dict(task.task_state or {})
            state.pop("validation_feedback", None)
            state.pop("validation_failures", None)
            state.pop("validation_messages", None)
            state["last_decision"] = proposal.model_dump(mode="json", exclude={"progress_draft"})
            task.task_state = state
            self.repo.append_event(
                task.id, "agent_decision",
                {"decision": proposal.decision, "summary": proposal.summary},
                visibility="internal", run_id=run.id,
            )

            if proposal.decision in {"search_capabilities", "describe_capability"}:
                self._record_discovery(task, run, proposal, broker)
                completed_this_run += 1
                continue

            if proposal.decision == "finish":
                if goals is None:
                    return self._finish_report(task, run, {
                        "status": proposal.report_status,
                        "summary": proposal.summary or "任务已完成。",
                        "risks": proposal.risks,
                        "unresolved": proposal.unresolved,
                    })
                return self._complete(
                    task, run, status=proposal.report_status,
                    summary=proposal.summary or "任务已完成。",
                    risks=proposal.risks, unresolved=proposal.unresolved,
                    model=last_model, token_usage=run_tokens,
                )
            if proposal.decision == "ask_user":
                if goals is not None:
                    # 只阻塞依赖该输入的步骤：还有独立步骤时继续推进，不冻结目标。
                    outcome = goals.on_step_awaiting_user(task, run, proposal.question)
                    if outcome == "advance":
                        self.repo.finish_run(
                            run,
                            summary={"blocked": "user", "question": proposal.question, "continued": True},
                            token_usage=run_tokens, model=last_model,
                        )
                        self.repo.transition_task(
                            task, TaskStatus.QUEUED.value,
                            event_type="goal_step_continued",
                            event_payload={"question": proposal.question},
                            visibility="conversation",
                        )
                        self.repo.enqueue_run(task, trigger="goal_step")
                        self.repo.checkpoint(task.id, reason="goal_step_continued")
                        self.db.commit()
                        return {"status": task.status, "task_id": task.id, "continued": True}
                self.repo.transition_task(
                    task, TaskStatus.BLOCKED_USER.value,
                    event_type="task_blocked_user",
                    event_payload={"question": proposal.question},
                    visibility="conversation",
                )
                self.repo.finish_run(
                    run, summary={"blocked": "user", "question": proposal.question},
                    token_usage=run_tokens, model=last_model,
                )
                self.repo.checkpoint(task.id, reason="blocked_user")
                self.db.commit()
                return {"status": task.status, "task_id": task.id, "question": proposal.question}
            if proposal.decision == "watch":
                self.repo.transition_task(
                    task, TaskStatus.BLOCKED_USER.value,
                    event_type="task_watching",
                    event_payload={"watch": proposal.watch},
                    visibility="conversation",
                )
                self.repo.finish_run(run, summary={"watching": proposal.watch}, token_usage=run_tokens, model=last_model)
                self.repo.checkpoint(task.id, reason="watching")
                self.db.commit()
                return {"status": task.status, "task_id": task.id}

            prepared = broker.prepare(task, proposal.capability_id, proposal.arguments)
            registration = prepared.registration
            current_hash = registration.safety.descriptor_hash if registration else "unavailable"
            stale = bool(registration and proposal.descriptor_hash and proposal.descriptor_hash != current_hash)
            block_reason = "descriptor_changed" if stale else (
                prepared.decision.reason if prepared.decision.outcome == "block" else None
            )
            action = self.repo.create_action(
                task=task,
                run=run,
                capability_id=proposal.capability_id,
                arguments=proposal.arguments,
                descriptor_hash=proposal.descriptor_hash or current_hash,
                risk_level=registration.safety.risk_level if registration else "critical",
                requires_approval=not stale and prepared.decision.outcome == "approval",
                preconditions=proposal.preconditions,
                effects=proposal.effects,
                target_node_id=registration.executor_node_id if registration and registration.executor_kind == "desktop_node" else None,
            )
            self.repo.transition_action(action, ActionStatus.VALIDATED.value)
            if block_reason is not None:
                self.repo.transition_action(
                    action, ActionStatus.FAILED.value,
                    error=block_reason,
                    payload={"reason": block_reason},
                )
                self.db.commit()
                completed_this_run += 1
                continue
            if prepared.decision.outcome == "approval":
                self.repo.transition_action(action, ActionStatus.AWAITING_APPROVAL.value)
                self.repo.transition_task(
                    task, TaskStatus.BLOCKED_APPROVAL.value,
                )
                self.repo.checkpoint(
                    task.id, reason="blocked_approval", pending_action_refs=[action.id],
                )
                approval = TaskApprovalService(self.db).create(task, action)
                self.repo.append_event(
                    task.id,
                    "task_blocked_approval",
                    {
                        "approval_id": approval.id, "action_id": action.id,
                        "capability_id": action.capability_id,
                        "arguments": action.arguments,
                        "risk_level": action.risk_level,
                        "preconditions": action.preconditions,
                        "effects": action.effects,
                    },
                    visibility="conversation",
                    run_id=run.id,
                    action_id=action.id,
                )
                self.repo.finish_run(
                    run, summary={"blocked": "approval", "approval_id": approval.id},
                    token_usage=run_tokens, model=last_model,
                )
                self.db.commit()
                return {
                    "status": task.status, "task_id": task.id,
                    "approval_id": approval.id, "action_id": action.id,
                }
            self.repo.transition_action(action, ActionStatus.READY.value)
            self.db.commit()

        # 单 Run 的推理/执行步数耗尽，checkpoint 后以新 Run 继续。
        self.db.refresh(task)
        if task.status == TaskStatus.CANCELLED.value:
            # 已取消的任务不回到 QUEUED，也不再创建新 Run。
            self.repo.finish_run(run, status=RunStatus.INTERRUPTED.value, error="task_cancelled")
            self.db.commit()
            return {"status": task.status, "task_id": task.id}
        budget_issue = self._budget_issue(task, budget, before_model=self.repo.ready_action(task.id) is None)
        if budget_issue:
            return self._stop_for_budget(task, run, budget_issue)
        if budget.max_runs is not None and int((task.usage or {}).get("runs", 0)) >= budget.max_runs:
            return self._stop_for_budget(task, run, "run_budget_exhausted")
        self.repo.finish_run(
            run, summary={"continued": True}, token_usage=run_tokens, model=last_model,
        )
        self.repo.transition_task(task, TaskStatus.QUEUED.value)
        self.repo.checkpoint(task.id, reason="run_action_limit")
        self.repo.enqueue_run(task, trigger="continue")
        self.db.commit()
        return {"status": task.status, "task_id": task.id, "continued": True}

    def _stop_for_budget(self, task: AgentTask, run: AgentRun, reason: str) -> dict[str, Any]:
        return self._finish_report(task, run, {
            "status": "partial", "summary": "", "stop_reason": reason,
            "budget_exhausted": reason.endswith("_budget_exhausted") or reason == "deadline_exceeded",
            "unresolved": [reason],
        })

    def _finish_report(self, task: AgentTask, run: AgentRun, outcome: dict[str, Any]) -> dict[str, Any]:
        from aiive.task_runtime.read_repository import TaskReadRepository
        from aiive.worker.outbox_execution_auth import require_execution_authorization

        def renew() -> bool:
            require_execution_authorization()
            self.repo.renew_run(run, lease_seconds=180)
            self.db.refresh(task)
            return task.status != TaskStatus.CANCELLED.value

        self.db.flush()  # Persist the final execution accounting before renew_run refreshes the row.
        active = renew()
        writer = getattr(self.agent, "generate_report", None)
        summary, usage, report_incomplete = "", {}, True
        if active:
            task.task_state = {**(task.task_state or {}), "pending_report": outcome}
            self.db.commit()
            if writer:
                summary, usage, report_incomplete = writer(task, outcome, renew=renew)
            active = renew()
        if not active:
            self.repo.finish_run(run, status=RunStatus.INTERRUPTED.value, error="task_cancelled",
                                 token_usage=run.token_usage, model=run.model)
            self.db.commit()
            return {"status": task.status, "task_id": task.id}

        state = dict(task.task_state or {})
        state.pop("pending_report", None)
        task.task_state = state
        if outcome.get("stop_reason"):
            ready = self.repo.ready_action(task.id)
            if ready is not None:
                self.repo.transition_action(ready, ActionStatus.INVALIDATED.value, error=outcome["stop_reason"])
            if report_incomplete:
                actions = TaskReadRepository(self.db).actions(task.id)
                counts = dict(self.db.query(AgentAction.status, func.count(AgentAction.id)).filter_by(
                    task_id=task.id,
                ).group_by(AgentAction.status).all())
                completed = [f"- #{item.sequence} {item.capability_id}" for item in reversed(actions)
                             if item.status == ActionStatus.SUCCEEDED.value]
                unfinished = [f"- #{item.sequence} {item.capability_id}（{item.status}）" for item in reversed(actions)
                              if item.status != ActionStatus.SUCCEEDED.value]
                fallback = [f"### 已完成（{counts.get(ActionStatus.SUCCEEDED.value, 0)} 个动作）",
                            *(completed or ["- 暂无可列出的成功动作。"]), "### 未完成或待核实",
                            *unfinished, "- 任务目标仍待完整验收。"]
                fallback.extend(f"- 待验收：{item}" for item in (task.task_brief or {}).get("acceptance_criteria", []))
                draft = state.get("progress_draft", "")
                if draft:
                    fallback.extend(["### 保存的草稿", str(draft)])
                summary = "\n\n".join(filter(None, [summary, "\n".join(fallback)]))
            suffix = "预算已达上限终止" if outcome.get("budget_exhausted") else "任务执行已终止"
            summary = f"{summary.rstrip()}\n\n{suffix}"
        else:
            summary = "\n\n".join(filter(None, [summary, outcome["summary"]])) if report_incomplete else summary

        return self._complete(
            task, run, status=outcome["status"], summary=summary,
            unresolved=outcome.get("unresolved"), risks=outcome.get("risks"),
            model=run.model, token_usage=run.token_usage, stop_reason=outcome.get("stop_reason"),
            budget_exhausted=outcome.get("budget_exhausted"), report_usage=usage or None,
            report_incomplete=True if report_incomplete and writer else None,
        )

    def _validation_failed(self, task: AgentTask, run: AgentRun, tokens: dict[str, int], model: str | None) -> dict[str, Any]:
        progress = self.db.query(AgentAction).filter_by(task_id=task.id, status=ActionStatus.SUCCEEDED.value).count()
        return self._complete(task, run, status="partial" if progress else "failed",
                               summary="模型输出或工具参数校验纠错已耗尽。",
                               unresolved=["agent_validation_exhausted"], token_usage=tokens, model=model, failed_run=True)

    def _record_discovery(
        self,
        task: AgentTask,
        run: AgentRun,
        proposal: ActionProposal,
        broker: CapabilityBroker,
    ) -> None:
        """处理只读的能力发现决策：不创建 Action、不调用任何工具 handler。

        发现结果只写入 task_state（id + 定义指纹），下一次组装上下文时按当前
        注册表重新解析；因此注册、卸载或定义变化会立刻反映到注入结果里。
        """
        state = dict(task.task_state or {})
        payload: dict[str, Any]
        if proposal.decision == "search_capabilities":
            namespace_ids = sorted(set(proposal.namespace_ids))
            query = proposal.query.strip()
            search = broker.search_capabilities(task, query, namespace_ids=namespace_ids, limit=proposal.limit)
            matched = [item["capability_id"] for item in search["tools"]]
            namespaces = [item["namespace_id"] for item in search["namespaces"]]
            discovery = record_search(
                state, query, matched, namespace_ids=namespace_ids, matched_namespace_ids=namespaces,
                loaded=search["loaded"],
            )
            payload = {
                "query": query,
                "namespace_ids": namespace_ids,
                "matched": matched,
                "matched_namespace_ids": namespaces,
                "loaded": [item["capability_id"] for item in search["loaded"]],
                "mode": search["mode"],
                "status": search["status"],
            }
            event_type = "tool_search"
        else:
            capability_id = proposal.capability_id.strip()
            definition = broker.describe_capability(task, capability_id)
            discovery = discovery_state(state)
            in_scope = bool(definition and definition.get("in_scope"))
            if definition is not None and in_scope:
                discovery = record_definition(state, capability_id, definition["descriptor_hash"])
            payload = {
                "capability_id": capability_id,
                "found": definition is not None,
                "in_scope": in_scope,
            }
            if definition is not None and not in_scope:
                payload["summary"] = definition
            event_type = "tool_describe"
        task.task_state = merge_state(state, discovery)
        usage = dict(task.usage or {})
        usage["discovery_steps"] = int(usage.get("discovery_steps", 0)) + 1
        task.usage = usage
        self.repo.append_event(
            task.id, event_type, payload, visibility="internal", run_id=run.id,
        )
        self.db.commit()

    def _dispatch_ready(self, task: AgentTask, run: AgentRun, action: AgentAction) -> str:
        self.repo.renew_run(run, lease_seconds=900)
        self.db.commit()
        broker = CapabilityBroker(self.db)
        issues = broker.validate_preconditions(action)
        if issues:
            self.repo.transition_action(
                action, ActionStatus.INVALIDATED.value,
                error=";".join(issues), payload={"issues": issues},
            )
            self.db.commit()
            return "continue"
        # Serialize dispatch intent with cancellation using Task -> Action lock order.
        locked_task = self.repo.get_task(task.id, for_update=True)
        if locked_task is None:
            raise RuntimeError("task_missing")
        task = locked_task
        self.db.refresh(action)
        if task.status == TaskStatus.CANCELLED.value or action.status == ActionStatus.CANCELLED.value:
            self.db.commit()
            return "terminal"
        action.execution_run_id = run.id
        action.execution_token = str(uuid.uuid4())
        self.repo.transition_action(action, ActionStatus.DISPATCHED.value)
        self.db.commit()
        result = broker.dispatch(task, run, action)
        # Broker 会 commit/reload 边界，重新绑定实体。
        refreshed_task = self.db.get(AgentTask, task.id)
        refreshed_action = self.db.get(AgentAction, action.id)
        if refreshed_task is None or refreshed_action is None:
            raise RuntimeError("task_or_action_disappeared_after_dispatch")
        self.db.refresh(refreshed_task)
        self.db.refresh(refreshed_action)
        task = refreshed_task
        action = refreshed_action
        outcome = action_outcome(result)
        error_type = str(result.get("error_type", ""))
        if outcome == "succeeded":
            evidence = self.evidence.capture_result(
                self.db, task_id=task.id, run_id=run.id, action=action, result=result,
            )
            self.repo.transition_action(
                action, ActionStatus.SUCCEEDED.value,
                payload={"evidence_id": evidence.id, "summary": evidence.summary},
            )
            self.repo.checkpoint(task.id, reason="action_succeeded")
            self.db.commit()
            return "continue"
        if outcome == "unknown":
            if action.capability_id == "run_shell":
                self.evidence.capture_result(
                    self.db, task_id=task.id, run_id=run.id, action=action, result=result,
                    kind="tool_error",
                )
            self.repo.transition_action(
                action, ActionStatus.UNKNOWN.value,
                error=str(result.get("error", "execution_unknown")),
            )
            if task.status == TaskStatus.CANCELLED.value:
                self.repo.finish_run(
                    run, status=RunStatus.INTERRUPTED.value,
                    summary={"cancelled_with_unknown_action": action.id},
                    error="task_cancelled",
                )
                self.repo.checkpoint(
                    task.id, reason="cancelled_execution_unknown", pending_action_refs=[action.id],
                )
                self.db.commit()
                return "terminal"
            self.repo.transition_task(
                task, TaskStatus.RECONCILING.value,
                event_type="task_reconciling",
                event_payload={"action_id": action.id, "node_id": action.target_node_id},
                visibility="conversation",
            )
            self.repo.finish_run(run, summary={"blocked": "reconciliation", "action_id": action.id})
            self.repo.checkpoint(task.id, reason="execution_unknown", pending_action_refs=[action.id])
            self.db.commit()
            return "blocked"
        if outcome == "cancelled":
            self.repo.transition_action(
                action, ActionStatus.CANCELLED.value,
                error=str(result.get("error") or "execution_cancelled"),
            )
        elif outcome == "invalidated":
            self.repo.transition_action(
                action, ActionStatus.INVALIDATED.value,
                error=str(result.get("error", "precondition_failed")),
            )
        else:
            usage = dict(task.usage or {})
            usage["tool_failures"] = int(usage.get("tool_failures", 0)) + 1
            task.usage = usage
            evidence = self.evidence.capture_result(
                self.db, task_id=task.id, run_id=run.id, action=action, result=result,
                kind="tool_error",
            )
            self.repo.transition_action(
                action, ActionStatus.FAILED.value,
                error=str(result.get("error", "execution_failed")),
                payload={
                    "evidence_id": evidence.id,
                    "error_type": error_type,
                    # 失败与"是否已产生外部影响"是两个独立事实，事件里都要保留。
                    "outcome_known": bool(result.get("outcome_known", True)),
                    "side_effect_started": bool(result.get("side_effect_started", False)),
                },
            )
        self.repo.checkpoint(task.id, reason="action_failed")
        self.db.commit()
        return "continue"

    def _complete(
        self,
        task: AgentTask,
        run: AgentRun | None,
        *,
        status: Literal["succeeded", "partial", "failed"],
        summary: str,
        unresolved: list[str] | None = None,
        risks: list[str] | None = None,
        model: str | None = None,
        token_usage: dict[str, int] | None = None,
        failed_run: bool = False,
        stop_reason: str | None = None,
        budget_exhausted: bool | None = None,
        report_usage: dict[str, int] | None = None,
        report_incomplete: bool | None = None,
    ) -> dict[str, Any]:
        task = self.db.get(AgentTask, task.id) or task
        progress = (task.task_state or {}).get("attachment_processing") or {}
        images = (task.task_state or {}).get("attachment_image_receipts") or {}
        required = (task.task_state or {}).get("required_attachment_ids", [])
        if status == "succeeded" and any(
            aid not in images and (
                not isinstance(progress.get(aid), dict)
                or int(progress[aid].get("total_chunks", 0)) <= 0
                or progress[aid].get("next_chunk_index") != progress[aid].get("total_chunks")
                or progress[aid].get("processed_chunk_count") != progress[aid].get("total_chunks")
            )
            for aid in required
        ):
            status = "partial"
            summary = "附件全文处理尚未完成。" + summary
            unresolved = [*(unresolved or []), "attachment_processing_incomplete"]
        if status == "succeeded" and run is not None:
            from aiive.supervisor.promotion_status import promotion_status
            from aiive.task_runtime.watchers import WatcherService

            latest = self.db.query(AgentAction).filter(
                AgentAction.task_id == task.id, AgentAction.capability_id.in_(["promote_slot", "run_selfdev_repair"]),
                AgentAction.status == ActionStatus.SUCCEEDED.value,
            ).order_by(AgentAction.sequence.desc()).first()
            deployment = (latest.result_summary or {}) if latest else {}
            request = deployment.get("promotion_request_id")
            if deployment.get("deployment_confirmation_required"):
                status = "partial"
                summary = "发布槽位已切换，当前服务运行版本待确认。"
                unresolved = [*(unresolved or []), "active_process_requires_verification"]
            if isinstance(request, str) and latest is not None:
                promotion = promotion_status(request)
                if promotion in {"requested", "observing"}:
                    started = latest.completed_at or utcnow()
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    deadline = started + timedelta(minutes=20)
                    if utcnow() < deadline:
                        WatcherService(self.db).wait_for_promotion(task, request, deadline)
                        self.repo.transition_task(task, TaskStatus.BLOCKED_USER.value, event_type="task_watching",
                                                  event_payload={"reason": "awaiting_promotion_result"}, visibility="conversation")
                        self.repo.finish_run(run, summary={"blocked": "promotion", "request_id": request},
                                             token_usage=token_usage, model=model)
                        self.repo.checkpoint(task.id, reason="awaiting_promotion_result")
                        self.db.commit()
                        return {"status": task.status, "task_id": task.id, "promotion": promotion}
                if promotion != "promoted":
                    status = "partial"
                    summary = {
                        "rolled_back": "部署已回滚。",
                        "failed": "部署未完成。",
                    }.get(promotion, "修复结果已保留，部署结果仍待确认。")
                    unresolved = [*(unresolved or []), f"promotion_{promotion}"]
        if run is not None and task.status == TaskStatus.QUEUED.value:
            self.repo.transition_task(task, TaskStatus.DISPATCHING.value)
            self.repo.transition_task(task, TaskStatus.RUNNING.value)
        evidence_ids = [row[0] for row in self.db.query(TaskEvidence.id).filter(TaskEvidence.task_id == task.id).all()]
        artifact_ids = [row[0] for row in self.db.query(TaskArtifact.id).filter(TaskArtifact.task_id == task.id).all()]
        succeeded = self.db.query(AgentAction).filter(
            AgentAction.task_id == task.id, AgentAction.status == ActionStatus.SUCCEEDED.value,
        ).count()
        report = TaskReport(
            status=status,
            summary=summary,
            completed_actions=succeeded,
            evidence_refs=evidence_ids,
            artifact_refs=artifact_ids,
            risks=list(dict.fromkeys(risks or [])),
            unresolved=list(dict.fromkeys(unresolved or [])),
            stop_reason=stop_reason,
            budget_exhausted=budget_exhausted,
            report_usage=report_usage,
            report_incomplete=report_incomplete,
        ).model_dump(mode="json", exclude_none=True)
        goals = GoalService(self.db) if GoalService.is_goal_task(task) else None
        if goals is not None:
            if status == "succeeded" and run is not None:
                decision = goals.on_step_finished(task, run, report)
                if decision in {"advance", "wait"}:
                    # 单步完成不等于目标完成：结算步骤后继续推进或进入等待。
                    self.repo.finish_run(
                        run, status=RunStatus.COMPLETED.value, summary=report,
                        token_usage=token_usage, model=model,
                    )
                    if decision == "advance":
                        self.repo.transition_task(
                            task, TaskStatus.QUEUED.value,
                            event_type="goal_step_completed",
                            event_payload={"progress": goals.detail(task)["summary"]["progress"]},
                            visibility="conversation",
                        )
                        self.repo.enqueue_run(task, trigger="goal_step")
                    else:
                        self.repo.transition_task(
                            task, TaskStatus.BLOCKED_USER.value,
                            event_type="goal_waiting",
                            event_payload={"reason": "waiting_next_step"},
                            visibility="conversation",
                        )
                    self.repo.checkpoint(task.id, reason=f"goal_{decision}")
                    self.db.commit()
                    return {"status": task.status, "task_id": task.id, "goal": decision}
            else:
                goals.on_goal_blocked(task, summary)
        # 目标钩子可能把任务重新排队/转入等待，因此 VERIFYING 只在真正收敛时进入。
        if run is not None and task.status == TaskStatus.QUEUED.value:
            self.repo.transition_task(task, TaskStatus.DISPATCHING.value)
            self.repo.transition_task(task, TaskStatus.RUNNING.value)
        self.repo.transition_task(task, TaskStatus.VERIFYING.value)
        final_status = {
            "succeeded": TaskStatus.SUCCEEDED.value,
            "partial": TaskStatus.PARTIAL.value,
            "failed": TaskStatus.FAILED.value,
        }[status]
        self.repo.transition_task(
            task, final_status, report=report,
            event_type="task_completed",
            event_payload=report,
            visibility="conversation",
        )
        if run is not None:
            self.repo.finish_run(
                run,
                status=RunStatus.FAILED.value if status == "failed" or failed_run else RunStatus.COMPLETED.value,
                summary=report,
                token_usage=token_usage,
                model=model,
                error=summary if status == "failed" or failed_run else None,
            )
        self.repo.checkpoint(task.id, reason=f"completed_{status}")
        self.db.commit()
        return {"status": task.status, "task_id": task.id, "report": report}

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = int(merged.get(key, 0)) + int(value or 0)
        return merged

    @staticmethod
    def _record_tokens(task: AgentTask, usage: dict[str, int]) -> None:
        current = dict(task.usage or {})
        current["prompt_tokens"] = int(current.get("prompt_tokens", 0)) + int(usage.get("prompt_tokens", 0))
        current["completion_tokens"] = int(current.get("completion_tokens", 0)) + int(usage.get("completion_tokens", 0))
        task.usage = current

    @staticmethod
    def _deadline_exceeded(budget: TaskBudget) -> bool:
        if not budget.deadline_at:
            return False
        try:
            deadline = datetime.fromisoformat(budget.deadline_at.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return deadline <= utcnow()
        except ValueError:
            return True

    @classmethod
    def _budget_issue(
        cls, task: AgentTask, budget: TaskBudget, *, before_model: bool,
    ) -> str | None:
        usage = task.usage or {}
        if before_model:
            if budget.max_actions is not None and int(usage.get("actions", 0)) >= budget.max_actions:
                return "action_budget_exhausted"
            if budget.max_model_calls is not None and int(usage.get("model_calls", 0)) >= budget.max_model_calls:
                return "model_call_budget_exhausted"
        total_tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        if budget.max_total_tokens is not None and total_tokens >= budget.max_total_tokens:
            return "token_budget_exhausted"
        if budget.max_tool_failures is not None and int(usage.get("tool_failures", 0)) >= budget.max_tool_failures:
            return "tool_failure_budget_exhausted"
        if budget.max_cost_usd is not None and float(usage.get("cost_usd", 0.0)) >= budget.max_cost_usd:
            return "cost_budget_exhausted"
        if budget.max_wall_time_seconds is not None:
            created_at = task.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if (utcnow() - created_at).total_seconds() >= budget.max_wall_time_seconds:
                return "wall_time_budget_exhausted"
        if cls._deadline_exceeded(budget):
            return "deadline_exceeded"
        return None

    def _ensure_isolation(self, task: AgentTask, run: AgentRun) -> None:
        """显式请求时一次性创建 worktree，并把写入 scope 收紧到该 workspace。"""
        brief = dict(task.task_brief or {})
        scope = dict(brief.get("scope") or {})
        if scope.get("isolation_mode") != "git_worktree":
            return
        state = dict(task.task_state or {})
        if state.get("worktree_path"):
            return
        roots = [str(value) for value in scope.get("allowed_roots", []) if value]
        if not roots:
            raise ValueError("git_worktree_requires_repository_root")
        from aiive.task_runtime.worktree import WorktreeIsolation

        repository_root = Path(roots[0]).expanduser().resolve()
        workspace = WorktreeIsolation(repository_root).create(task.id)
        scope["allowed_roots"] = [str(workspace), *roots[1:]]
        brief["scope"] = scope
        task.task_brief = brief
        state["worktree_path"] = str(workspace)
        state["source_repository"] = str(repository_root)
        task.task_state = state
        self.repo.append_event(
            task.id,
            "isolated_workspace_created",
            {"path": str(workspace), "source_repository": str(repository_root)},
            visibility="task",
            run_id=run.id,
        )

    @staticmethod
    def _deterministic_proposal(task: AgentTask) -> ActionProposal | None:
        if task.executor_type != "deterministic":
            return None
        if (task.task_state or {}).get("deterministic_action_proposed"):
            return None
        initial = (task.task_brief or {}).get("initial_input") or {}
        raw = initial.get("deterministic_action") if isinstance(initial, dict) else None
        if not isinstance(raw, dict):
            return None
        return ActionProposal.model_validate({"decision": "action", **raw})
