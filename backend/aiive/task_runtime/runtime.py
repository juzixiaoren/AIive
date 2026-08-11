"""Persistent Task Runtime：每次 wake 创建 AgentRun，并推进有界 Action 循环。"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from aiive.control.approval_service import TaskApprovalService
from aiive.control.capability_broker import CapabilityBroker
from aiive.db.models import AgentAction, AgentRun, AgentTask, TaskArtifact, TaskEvidence
from aiive.task_runtime.agent_executor import TaskAgentExecutor
from aiive.task_runtime.evidence import EvidenceStore
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import (
    ActionProposal,
    ActionStatus,
    RunStatus,
    TaskBudget,
    TaskReport,
    TaskStatus,
)

logger = logging.getLogger(__name__)


class PersistentTaskRuntime:
    def __init__(
        self,
        db: Session,
        *,
        agent_executor: TaskAgentExecutor | None = None,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self.db = db
        self.repo = TaskRepository(db)
        self.agent = agent_executor or TaskAgentExecutor(db)
        self.evidence = evidence_store or EvidenceStore()

    def run(self, task_id: str, *, trigger: str = "dispatch") -> dict[str, Any]:
        task, run = self.repo.start_run(task_id, trigger=trigger)
        self._ensure_isolation(task, run)
        self.db.commit()
        budget = TaskBudget.model_validate(task.budgets or {})
        initial_budget_issue = self._budget_issue(task, budget, before_model=False)
        if initial_budget_issue:
            return self._complete(
                task, run, status="partial",
                summary="任务运行预算或截止时间已耗尽。",
                unresolved=[initial_budget_issue],
            )

        completed_this_run = 0
        last_model: str | None = None
        run_tokens: dict[str, int] = {}
        while completed_this_run < budget.max_actions_per_run:
            self.db.refresh(task)
            if task.status == TaskStatus.CANCELLED.value:
                self.repo.finish_run(run, status=RunStatus.INTERRUPTED.value, error="task_cancelled")
                self.db.commit()
                return {"status": task.status, "task_id": task.id}
            ready = self.repo.ready_action(task.id)
            if ready is not None:
                outcome = self._dispatch_ready(task, run, ready)
                completed_this_run += 1
                if outcome in {"blocked", "terminal"}:
                    return {"status": task.status, "task_id": task.id, "action_id": ready.id}
                continue

            budget_issue = self._budget_issue(task, budget, before_model=True)
            if budget_issue:
                return self._complete(
                    task, run, status="partial",
                    summary="任务预算已耗尽。",
                    unresolved=[budget_issue],
                    model=last_model, token_usage=run_tokens,
                )

            broker = CapabilityBroker(self.db)
            capabilities = broker.list_capabilities(task)
            if not capabilities:
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

            deterministic = self._deterministic_proposal(task)
            if deterministic is not None:
                proposal = deterministic
                state = dict(task.task_state or {})
                state["deterministic_action_proposed"] = True
                task.task_state = state
                run.input_snapshot = {
                    "task_id": task.id,
                    "executor_type": "deterministic",
                    "capability_ids": [item.get("capability_id") for item in capabilities],
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
                decision = self.agent.decide(task, capabilities)
                last_model = decision.model
                run_tokens = self._merge_usage(run_tokens, decision.token_usage)
                self._record_tokens(task, decision.token_usage)
                run.input_snapshot = {
                    **decision.input_snapshot,
                    "prompt_refs": decision.prompt_refs,
                }
                proposal = decision.proposal
            state = dict(task.task_state or {})
            state["last_decision"] = proposal.model_dump(mode="json")
            task.task_state = state
            self.repo.append_event(
                task.id, "agent_decision",
                {"decision": proposal.decision, "summary": proposal.summary},
                visibility="internal", run_id=run.id,
            )

            if proposal.decision == "finish":
                return self._complete(
                    task, run, status=proposal.report_status,
                    summary=proposal.summary or "任务已完成。",
                    model=last_model, token_usage=run_tokens,
                )
            if proposal.decision == "ask_user":
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
                from aiive.task_runtime.watchers import WatcherService

                WatcherService(self.db).create_from_proposal(task, proposal.watch)
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

            if int((task.usage or {}).get("actions", 0)) >= budget.max_actions:
                return self._complete(
                    task, run, status="partial",
                    summary="任务 Action 预算已耗尽。",
                    unresolved=["action_budget_exhausted"],
                    model=last_model, token_usage=run_tokens,
                )

            prepared = broker.prepare(task, proposal.capability_id, proposal.arguments)
            registration = prepared.registration
            action = self.repo.create_action(
                task=task,
                run=run,
                capability_id=proposal.capability_id,
                arguments=proposal.arguments,
                descriptor_hash=registration.safety.descriptor_hash if registration else "unavailable",
                risk_level=registration.safety.risk_level if registration else "critical",
                requires_approval=prepared.decision.outcome == "approval",
                preconditions=proposal.preconditions,
                effects=proposal.effects,
                target_node_id=registration.executor_node_id if registration and registration.executor_kind == "desktop_node" else None,
            )
            self.repo.transition_action(action, ActionStatus.VALIDATED.value)
            if prepared.decision.outcome == "block":
                self.repo.transition_action(
                    action, ActionStatus.FAILED.value,
                    error=prepared.decision.reason,
                    payload={"reason": prepared.decision.reason},
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
        self.repo.finish_run(
            run, summary={"continued": True}, token_usage=run_tokens, model=last_model,
        )
        self.repo.transition_task(task, TaskStatus.QUEUED.value)
        self.repo.checkpoint(task.id, reason="run_action_limit")
        self.repo.enqueue_run(task, trigger="continue")
        self.db.commit()
        return {"status": task.status, "task_id": task.id, "continued": True}

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
        task = refreshed_task
        action = refreshed_action
        error_type = str(result.get("error_type", ""))
        if result.get("ok"):
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
        if error_type in {"execution_unknown", "desktop_operation_timeout", "desktop_node_disconnected"}:
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
        if error_type == "precondition_failed":
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
                payload={"evidence_id": evidence.id, "error_type": error_type},
            )
        self.repo.checkpoint(task.id, reason="action_failed")
        self.db.commit()
        return "continue"

    def _complete(
        self,
        task: AgentTask,
        run: AgentRun,
        *,
        status: Literal["succeeded", "partial", "failed"],
        summary: str,
        unresolved: list[str] | None = None,
        model: str | None = None,
        token_usage: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        task = self.db.get(AgentTask, task.id) or task
        if task.status == TaskStatus.QUEUED.value:
            self.repo.transition_task(task, TaskStatus.DISPATCHING.value)
            self.repo.transition_task(task, TaskStatus.RUNNING.value)
        self.repo.transition_task(task, TaskStatus.VERIFYING.value)
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
            unresolved=unresolved or [],
        ).model_dump(mode="json")
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
        self.repo.finish_run(
            run,
            status=RunStatus.FAILED.value if status == "failed" else RunStatus.COMPLETED.value,
            summary=report,
            token_usage=token_usage,
            model=model,
            error=summary if status == "failed" else None,
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
        if int(usage.get("runs", 0)) > budget.max_runs:
            return "run_budget_exhausted"
        if before_model and int(usage.get("model_calls", 0)) >= budget.max_model_calls:
            return "model_call_budget_exhausted"
        total_tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        if total_tokens >= budget.max_total_tokens:
            return "token_budget_exhausted"
        if int(usage.get("tool_failures", 0)) >= budget.max_tool_failures:
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
