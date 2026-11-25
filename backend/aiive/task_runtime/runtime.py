"""Persistent Task Runtime：每次 wake 创建 AgentRun，并推进有界 Action 循环。"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from aiive.control.approval_service import TaskApprovalService
from aiive.control.capability_broker import CapabilityBroker
from aiive.control.tool_trust import ToolTrustPolicy
from aiive.db.models import AgentAction, AgentRun, AgentTask, TaskArtifact, TaskEvidence
from aiive.task_runtime.agent_executor import TaskAgentExecutor
from aiive.task_runtime.errors import AgentOutputValidationError, TaskModelBudgetError, TaskNotRunnableError
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
    MAX_DISCOVERY_QUERIES,
    build_tool_view,
    discovery_state,
    merge_state,
    normalize_query,
    record_definition,
    record_search,
    worker_resident_ids,
)
from aiive.control.scope import TaskScope
from aiive.task_runtime.router import with_selfdev_scope
from aiive.tools.base import outcome_is_unknown
from aiive.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)
NO_PROGRESS_LIMIT = 3
CODING_NO_PROGRESS_LIMIT = 8


def action_outcome(result: dict[str, Any]) -> str:
    """把工具执行 payload 映射为 Action 终态语义。

    成功 / 确定性失败 / 已确认取消 / 外部副作用未知必须互斥，未确认的结果
    一律优先判为 unknown，不能被降级成成功或普通失败。
    """
    if outcome_is_unknown(result):
        return "unknown"
    if result.get("ok"):
        return "succeeded"
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
        already_stalled = False
        if candidate is not None and candidate.status == TaskStatus.QUEUED.value:
            budget = TaskBudget.from_stored(candidate.budgets)
            pending_report = (candidate.task_state or {}).get("pending_report")
            already_stalled = len((candidate.task_state or {}).get("no_progress") or []) >= self._progress_limit(candidate)
            if budget.max_runs is not None and int((candidate.usage or {}).get("runs", 0)) >= budget.max_runs:
                initial_budget_issue = "run_budget_exhausted"
            else:
                initial_budget_issue = self._budget_issue(
                    candidate, budget, before_model=self.repo.ready_action(candidate.id) is None,
                )
        report_only = pending_report is not None or initial_budget_issue is not None or already_stalled
        task, run = self.repo.start_run(
            task_id, trigger="report" if report_only else trigger, count_toward_budget=not report_only,
        )
        self.active_run_id = run.id
        self.db.commit()
        if pending_report is not None:
            return self._finish_report(task, run, pending_report)
        if stalled := self._stop_if_stalled(task, run):
            return stalled
        if initial_budget_issue:
            return self._stop_for_budget(task, run, initial_budget_issue)
        task.task_brief = with_selfdev_scope(task.task_type, task.task_brief or {})
        try:
            self._ensure_isolation(task, run)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return self._complete(task, run, status="failed", summary="无法创建任务工作区。",
                                  unresolved=[str(exc)], stop_reason="workspace_unavailable")
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

        def run_step() -> dict[str, Any] | None:
            nonlocal correction_failures, completed_this_run, last_model, run_tokens

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
            stalled = self._stop_if_stalled(task, run)
            if stalled:
                return stalled
            ready = self.repo.ready_action(task.id)
            budget_issue = self._budget_issue(task, budget, before_model=ready is None)
            if budget_issue:
                return self._stop_for_budget(task, run, budget_issue)
            if ready is not None:
                outcome = self._dispatch_ready(task, run, ready, token_usage=run_tokens, model=last_model)
                completed_this_run += 1
                if isinstance(outcome, dict):
                    return outcome
                if outcome in {"blocked", "terminal"}:
                    return {"status": task.status, "task_id": task.id, "action_id": ready.id}
                return None

            broker = CapabilityBroker(self.db)
            available = broker.available_registry(task)
            catalog = available.catalog()
            entries = {item["capability_id"]: item for item in catalog}
            resident = [entries[key] for key in worker_resident_ids(task.task_type, has_workspace=bool((task.task_state or {}).get("worktree_path"))) if key in entries]
            deterministic = self._deterministic_proposal(task)
            if int((task.usage or {}).get("model_calls", 0)) == 0:
                brief = task.task_brief or {}
                scope = TaskScope.from_brief(brief)
                required = set(brief.get("required_capabilities") or [])
                if deterministic is not None:
                    required.add(deterministic.capability_id)
                missing = [
                    f"{'policy_denied' if key in entries else 'capability_unavailable'}:{key}"
                    for key in sorted(required) if key not in entries or not scope.allows_capability(key)
                ]
                for key in sorted(required & entries.keys()):
                    if not scope.allows_capability(key):
                        continue
                    registration = available.get(key)
                    if registration is not None and registration.safety.uses_network and not scope.allow_network:
                        missing.append(f"policy_denied:{key}:network_access_not_in_task_scope")
                if not missing and scope.allowed_capabilities and not (scope.allowed_capabilities & entries.keys()):
                    missing = [f"capability_unavailable:{key}" for key in sorted(scope.allowed_capabilities)]
                if missing or not catalog:
                    return self._complete(
                        task, run, status="partial", summary="启动任务所需的执行能力不可用。",
                        unresolved=missing or ["capability_unavailable:no_registered_tools"],
                        stop_reason="required_capability_unavailable",
                    )

            decision = None
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
                    payload = error.payload()
                    task.task_state = {**(task.task_state or {}), "validation_feedback": payload,
                                       "validation_failures": correction_failures, "validation_messages": error.feedback}
                    if not correction_allowed(correction_failures):
                        return self._validation_failed(task, run, run_tokens, last_model)
                    budget_issue = self._budget_issue(task, budget, before_model=True)
                    if budget_issue:
                        return self._stop_for_budget(task, run, budget_issue)
                    self.db.commit()
                    return None
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
                        self._record_outcome(task, "invalid_proposal", proposal.capability_id, "invalid_tool_arguments")
                        if stalled := self._stop_if_stalled(task, run):
                            return stalled
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
                        return None
                    proposal.arguments = validation.validated_params
            if proposal.decision == "watch":
                from aiive.task_runtime.watchers import WatcherService

                try:
                    WatcherService(self.db).create_from_proposal(task, proposal.watch)
                except (ValueError, OSError, RuntimeError) as error:
                    correction_failures += 1
                    self._record_outcome(task, "invalid_proposal", "task_watch", str(error))
                    if stalled := self._stop_if_stalled(task, run):
                        return stalled
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
                    return None
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
                self._record_discovery(task, run, proposal, broker, available, resident)
                completed_this_run += 1
                return None

            if proposal.decision == "finish" and proposal.report_status == "succeeded":
                proposal = self._verify_selfdev_delivery(task, proposal)
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
                outcome = (
                    "definition_changed" if stale else
                    "capability_unavailable" if prepared.decision.reason in {"unknown_capability", "capability_disabled"}
                    else "policy_denied"
                )
                self._record_outcome(task, outcome, proposal.capability_id, block_reason)
                self.repo.transition_action(
                    action, ActionStatus.FAILED.value,
                    error=block_reason,
                    payload={"reason": block_reason, "outcome": outcome, "error_type": outcome},
                )
                self.db.commit()
                completed_this_run += 1
                return None
            if prepared.decision.outcome == "approval":
                return self._request_approval(task, run, action, token_usage=run_tokens, model=last_model)
            self.repo.transition_action(action, ActionStatus.READY.value)
            self.db.commit()
            return None

        while completed_this_run < (budget.max_actions_per_run or 8):
            result = run_step()
            if result is not None:
                return result

        # 单 Run 的推理/执行步数耗尽，checkpoint 后以新 Run 继续。
        self.db.refresh(task)
        if task.status == TaskStatus.CANCELLED.value:
            # 已取消的任务不回到 QUEUED，也不再创建新 Run。
            self.repo.finish_run(run, status=RunStatus.INTERRUPTED.value, error="task_cancelled")
            self.db.commit()
            return {"status": task.status, "task_id": task.id}
        stalled = self._stop_if_stalled(task, run)
        if stalled:
            return stalled
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

    @staticmethod
    def _progress_limit(task: AgentTask) -> int:
        from aiive.task_runtime.router import CODING_TASK_TYPES

        scope = (task.task_brief or {}).get("scope") or {}
        coding = task.task_type in CODING_TASK_TYPES or scope.get("isolation_mode") == "git_worktree"
        return CODING_NO_PROGRESS_LIMIT if coding else NO_PROGRESS_LIMIT

    @staticmethod
    def _record_outcome(
        task: AgentTask, outcome: str, subject: str, reason: str, *, progress: bool = False,
    ) -> None:
        state = dict(task.task_state or {})
        recent = state.get("no_progress") or []
        if progress:
            state.pop("workspace_observations", None)
        state["no_progress"] = [] if progress else [
            *recent[-(PersistentTaskRuntime._progress_limit(task) - 1):], f"{outcome}:{subject}:{reason}"[:600],
        ]
        task.task_state = state
        if outcome in {"policy_denied", "capability_unavailable", "execution_failed",
                       "definition_changed", "precondition_failed"}:
            usage = dict(task.usage or {})
            usage["tool_failures"] = int(usage.get("tool_failures", 0)) + 1
            task.usage = usage

    def _stop_if_stalled(self, task: AgentTask, run: AgentRun) -> dict[str, Any] | None:
        recent = (task.task_state or {}).get("no_progress") or []
        if len(recent) < self._progress_limit(task):
            return None
        return self._complete(
            task, run, status="partial", summary=f"连续 {self._progress_limit(task)} 步未取得进展，任务已终止。",
            unresolved=recent, stop_reason="no_progress",
            model=run.model, token_usage=run.token_usage,
        )

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
        issues = ((task.task_state or {}).get("validation_feedback") or {}).get("issues", [])
        unresolved = ["agent_validation_exhausted", *[issue["code"] for issue in issues if "code" in issue]]
        summary = "模型输出或工具参数校验纠错已耗尽。"
        if task.executor_type == "deterministic":
            return self._complete(task, run, status="partial", summary=summary,
                                  unresolved=unresolved, token_usage=tokens, model=model, failed_run=True)
        run.token_usage = tokens
        run.model = model
        return self._finish_report(task, run, {
            "status": "partial", "summary": summary, "unresolved": unresolved,
            "stop_reason": "agent_validation_exhausted",
        })

    def _record_discovery(
        self,
        task: AgentTask,
        run: AgentRun,
        proposal: ActionProposal,
        broker: CapabilityBroker,
        available: ToolRegistry,
        resident: list[dict[str, Any]],
    ) -> None:
        """处理只读的能力发现决策：不创建 Action、不调用任何工具 handler。

        发现结果只写入 task_state（id + 定义指纹），下一次组装上下文时按当前
        注册表重新解析；因此注册、卸载或定义变化会立刻反映到注入结果里。
        """
        state = dict(task.task_state or {})
        discovery = discovery_state(state)
        usage = dict(task.usage or {})
        execution = broker.registry_for(task, available=available)
        seen = dict(state.get("seen_tool_definitions") or {})

        def bound_definitions() -> dict[str, str]:
            view = build_tool_view(execution, resident, discovery, discovery_registry=available)
            return {
                item["capability_id"]: item["descriptor_hash"]
                for group in ("resident", "available", "loaded")
                for item in view[group] if "input_schema" in item
            }

        seen.update(bound_definitions())
        outcome = "no_progress"
        payload: dict[str, Any]
        if proposal.decision == "search_capabilities":
            namespace_ids = sorted(set(proposal.namespace_ids))
            query = normalize_query(proposal.query)
            subject = query
            count = int(usage.get("discovery_queries", len(discovery["queries"])))
            fingerprint = hashlib.sha256(json.dumps(
                sorted((item["capability_id"], item["descriptor_hash"], execution.get(item["capability_id"]) is not None)
                       for item in available.catalog()),
            ).encode()).hexdigest()
            searched = dict(state.get("searched_tool_queries") or {})
            key = json.dumps([query, namespace_ids], ensure_ascii=False)
            already_searched = key not in searched and any(
                normalize_query(str(item.get("query") or "")) == query
                and (item.get("namespace_ids") or []) == namespace_ids for item in discovery["queries"]
            )
            payload = {"query": query, "namespace_ids": namespace_ids}
            if searched.get(key) == fingerprint or already_searched:
                reason = "duplicate_query"
            elif count >= MAX_DISCOVERY_QUERIES:
                reason = "discovery_query_budget_exhausted"
            else:
                search = broker.search_capabilities(
                    task, query, namespace_ids=namespace_ids, limit=proposal.limit, available=available,
                )
                usage["discovery_queries"] = count + 1
                searched[key] = fingerprint
                state["searched_tool_queries"] = searched
                matched = [item["capability_id"] for item in search["tools"]]
                namespaces = [item["namespace_id"] for item in search["namespaces"]]
                discovery = record_search(
                    state, query, matched, namespace_ids=namespace_ids, matched_namespace_ids=namespaces,
                    loaded=search["loaded"],
                )
                payload.update(matched=matched, matched_namespace_ids=namespaces, mode=search["mode"],
                               status=search["status"])
                if not matched:
                    outcome, reason = "capability_unavailable", "no_search_results"
                elif not any(item["in_scope"] for item in search["tools"]):
                    outcome, reason = "policy_denied", "search_results_out_of_scope"
                else:
                    reason = "no_new_definition"
            event_type = "tool_search"
        else:
            capability_id = proposal.capability_id.strip()
            subject = capability_id
            definition = broker.describe_capability(task, capability_id, available=available)
            in_scope = bool(definition and definition.get("in_scope"))
            if definition is not None and in_scope:
                discovery = record_definition(state, capability_id, definition["descriptor_hash"])
                reason = "no_new_definition"
            elif definition is not None:
                outcome, reason = "policy_denied", "capability_not_in_task_scope"
            else:
                outcome, reason = "capability_unavailable", "unknown_capability"
            payload = {
                "capability_id": capability_id,
                "found": definition is not None,
                "in_scope": in_scope,
            }
            if definition is not None and not in_scope:
                payload["summary"] = definition
            event_type = "tool_describe"
        bound = bound_definitions()
        loaded = [key for key, descriptor in bound.items() if seen.get(key) != descriptor]
        state["seen_tool_definitions"] = {**seen, **bound}
        if loaded:
            outcome, reason = "succeeded", "new_definition_loaded"
        payload.update(outcome=outcome, reason=reason, loaded=loaded)
        task.task_state = merge_state(state, discovery)
        usage["discovery_steps"] = int(usage.get("discovery_steps", 0)) + 1
        task.usage = usage
        self._record_outcome(task, outcome, subject, reason, progress=bool(loaded))
        self.repo.append_event(
            task.id, event_type, payload, visibility="internal", run_id=run.id,
        )
        self.db.commit()

    def _request_approval(
        self, task: AgentTask, run: AgentRun, action: AgentAction, *,
        token_usage: dict[str, int] | None = None, model: str | None = None,
    ) -> dict[str, Any]:
        action.requires_approval = True
        self.repo.transition_action(action, ActionStatus.AWAITING_APPROVAL.value)
        self.repo.transition_task(task, TaskStatus.BLOCKED_APPROVAL.value)
        self.repo.checkpoint(task.id, reason="blocked_approval", pending_action_refs=[action.id])
        approval = TaskApprovalService(self.db).create(task, action)
        self.repo.append_event(
            task.id, "task_blocked_approval",
            {
                "approval_id": approval.id, "action_id": action.id,
                "capability_id": action.capability_id, "arguments": action.arguments,
                "risk_level": action.risk_level, "preconditions": action.preconditions,
                "effects": action.effects,
            },
            visibility="conversation", run_id=run.id, action_id=action.id,
        )
        self.repo.finish_run(
            run, summary={"blocked": "approval", "approval_id": approval.id},
            token_usage=token_usage, model=model,
        )
        self.db.commit()
        return {"status": task.status, "task_id": task.id, "approval_id": approval.id, "action_id": action.id}

    def _dispatch_ready(
        self, task: AgentTask, run: AgentRun, action: AgentAction, *,
        token_usage: dict[str, int] | None = None, model: str | None = None,
    ) -> str | dict[str, Any]:
        self.repo.renew_run(run, lease_seconds=900)
        self.db.commit()
        broker = CapabilityBroker(self.db)
        issues = broker.validate_preconditions(action)
        if issues:
            self._record_outcome(task, "precondition_failed", action.capability_id, ";".join(issues))
            self.repo.transition_action(
                action, ActionStatus.INVALIDATED.value,
                error=";".join(issues), payload={"issues": issues, "outcome": "precondition_failed"},
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
        # Freeze the mode at dispatch intent. The broker still rechecks tool identity and execution constraints.
        trust = ToolTrustPolicy.load(self.db, lock_for_dispatch=True)
        prepared = broker.prepare(task, action.capability_id, action.arguments or {}, trust=trust)
        if (
            prepared.decision.outcome == "approval"
            and prepared.registration is not None
            and prepared.registration.safety.descriptor_hash == action.descriptor_hash
            and not broker.has_approval(action)
        ):
            return self._request_approval(task, run, action, token_usage=token_usage, model=model)
        action.execution_run_id = run.id
        action.execution_token = str(uuid.uuid4())
        self.repo.transition_action(action, ActionStatus.DISPATCHED.value)
        self.db.commit()
        result = broker.dispatch(task, run, action, trust=trust)
        # Broker 会 commit/reload 边界，重新绑定实体。
        refreshed_task = self.db.get(AgentTask, task.id)
        refreshed_action = self.db.get(AgentAction, action.id)
        if refreshed_task is None or refreshed_action is None:
            raise RuntimeError("task_or_action_disappeared_after_dispatch")
        self.db.refresh(refreshed_task)
        self.db.refresh(refreshed_action)
        if refreshed_task.current_run_id != run.id or refreshed_action.status != ActionStatus.DISPATCHED.value:
            raise TaskNotRunnableError("action_dispatch_owner_changed")
        task = refreshed_task
        action = refreshed_action
        outcome = action_outcome(result)
        error_type = str(result.get("error_type", ""))
        if action.capability_id == "run_selfdev_repair":
            stage = result.get("result") or result.get("details") or {}
            if stage.get("workspace_hash"):
                task.task_state = {**(task.task_state or {}), "workspace_delivery": {
                    "hash": stage["workspace_hash"], "verified": outcome == "succeeded",
                }}
        if outcome == "succeeded":
            if not (task.task_state or {}).get("worktree_path"):
                self._record_outcome(task, outcome, action.capability_id, "", progress=True)
            else:
                payload = result.get("result") or {}
                changed = isinstance(payload, dict) and (
                    payload.get("changed") is True
                    or (action.capability_id in {"run_selfdev_repair", "promote_slot"} and payload.get("ok") is True)
                )
                shell_live = (action.capability_id == "shell_session" and isinstance(payload, dict)
                              and payload.get("status") in {"starting", "running"})
                if changed:
                    self._record_outcome(task, outcome, action.capability_id, "", progress=True)
                elif not shell_live:
                    # New observations are neutral. Only actual changes clear the streak.
                    state = dict(task.task_state or {})
                    seen = state.get("workspace_observations") or []
                    key = f"{action.capability_id}:{action.arguments_hash}"
                    if key in seen:
                        self._record_outcome(task, "repeated_observation", action.capability_id, "")
                    else:
                        task.task_state = {**state, "workspace_observations": [*seen[-127:], key]}
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
                payload={"outcome": "execution_unknown", "error_type": error_type},
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
            self._record_outcome(task, "precondition_failed", action.capability_id, str(result.get("error", "")))
            self.repo.transition_action(
                action, ActionStatus.INVALIDATED.value,
                error=str(result.get("error", "precondition_failed")),
                payload={"outcome": "precondition_failed", "error_type": error_type},
            )
        else:
            category = {
                "unknown_tool": "capability_unavailable", "capability_disabled": "capability_unavailable",
                "policy_denied": "policy_denied", "access_denied": "policy_denied",
                "descriptor_changed": "definition_changed",
            }.get(error_type, "execution_failed")
            self._record_outcome(task, category, action.capability_id, str(result.get("error", error_type)))
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
                    "outcome": category,
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
            path = state["worktree_path"]
            scope["allowed_roots"] = [path, *(root for root in scope.get("allowed_roots", [])
                                              if root not in {path, state.get("source_repository")})]
            task.task_brief = {**brief, "scope": scope}
            return
        roots = [str(value) for value in scope.get("allowed_roots", []) if value]
        if not roots:
            raise ValueError("git_worktree_requires_repository_root")
        from aiive.task_runtime.router import SELFDEV_TASK_TYPES
        from aiive.task_runtime.workspace import create_workspace

        repository_root = Path(roots[0]).expanduser().resolve()
        selfdev = task.task_type in SELFDEV_TASK_TYPES
        try:
            workspace, _ = create_workspace(task.id, repository_root, selfdev=selfdev)
        except (OSError, subprocess.SubprocessError):
            # A partial Git checkout can be reused on the next attempt.
            logger.info("Retrying interrupted workspace creation for %s", task.id, exc_info=True)
            workspace, _ = create_workspace(task.id, repository_root, selfdev=selfdev)
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
    def _verify_selfdev_delivery(task: AgentTask, proposal: ActionProposal) -> ActionProposal:
        from aiive.task_runtime.router import SELFDEV_TASK_TYPES
        from aiive.task_runtime.workspace import changes, inventory, read_metadata, snapshot_hash

        state = task.task_state or {}
        if task.task_type not in SELFDEV_TASK_TYPES or not state.get("worktree_path"):
            return proposal
        workspace = Path(state["worktree_path"])
        metadata = read_metadata(workspace)
        files = inventory(workspace, git=metadata["git"])
        if not changes(workspace, metadata, files):
            return proposal
        previous = state.get("workspace_delivery") or {}
        if previous.get("hash") != snapshot_hash(files):
            return ActionProposal(decision="action", capability_id="run_selfdev_repair",
                                  arguments={"goal": task.goal}, summary="验证任务工作区并同步候选槽位")
        if not previous.get("verified"):
            return proposal.model_copy(update={
                "report_status": "partial",
                "unresolved": [*proposal.unresolved, "workspace_candidate_unverified"],
            })
        return proposal

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
