"""长期目标生命周期：提案确认、步骤推进、定时触发、沉默策略与完成校验。

设计约束（复用优先）：
- 目标本身仍是一条 AgentTask（task_type=long_term_goal），步骤存在 task_state["goal"]，
  不新建任务引擎、不新增表；Checkpoint 继续保存 plan 快照，重启后可恢复。
- 定时触发复用 AgentTaskWatch（time watcher）+ Outbox agent_task_run，唤醒幂等由
  watch 状态与 operation_id 双重保证。
- 交互步骤到期只写通知（Event.notification_created），不自动开跑、不打断前台对话。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from sqlalchemy.orm import Session

from aiive.config import settings
from aiive.db.models import AgentRun, AgentTask, AgentTaskWatch, Event, Thread
from aiive.task_runtime.goal_plan import (
    FINISHED_STEP_STATES,
    GOAL_TASK_TYPE,
    GoalPlan,
    GoalStatus,
    GoalStep,
    GoalStepKind,
    GoalStepStatus,
    dependencies_met,
    next_occurrence,
    trigger_of,
    next_runnable_step,
    parse_datetime,
    plan_complete,
    summarize,
    upcoming_steps,
)
from aiive.task_runtime.repository import TaskRepository, utcnow
from aiive.task_runtime.schemas import TaskStatus

logger = logging.getLogger(__name__)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class GoalError(ValueError):
    """目标生命周期中的可预期拒绝（状态不允许、计划不存在等）。"""


class GoalService:
    """目标计划的服务端唯一写入口。"""

    def __init__(self, db: Session):
        self.db: Session = db
        self.repo: TaskRepository = TaskRepository(db)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    @staticmethod
    def is_goal_task(task: AgentTask) -> bool:
        return task.task_type == GOAL_TASK_TYPE and isinstance((task.task_state or {}).get("goal"), dict)

    def load_plan(self, task: AgentTask) -> GoalPlan | None:
        raw = (task.task_state or {}).get("goal")
        if not isinstance(raw, dict):
            return None
        try:
            return GoalPlan.model_validate(raw)
        except Exception:  # noqa: BLE001 - 计划被外部写坏时不能拖垮任务
            logger.warning("目标计划解析失败: task_id=%s", task.id, exc_info=True)
            return None

    def detail(self, task: AgentTask) -> dict[str, Any]:
        plan = self.load_plan(task)
        if plan is None:
            raise GoalError("goal_not_found")
        return {
            "task_id": task.id,
            "thread_id": task.thread_id,
            "task_status": task.status,
            "plan": plan.model_dump(mode="json"),
            "summary": summarize(plan),
            "next_wake_at": task.wake_at,
        }

    # ------------------------------------------------------------------
    # 提案与确认
    # ------------------------------------------------------------------
    def attach_plan(self, task: AgentTask, raw_plan: dict[str, Any]) -> GoalPlan:
        """把计划挂到新建任务上，并让任务停在“等待用户确认”。"""
        now = utcnow()
        plan = GoalPlan.model_validate({
            **raw_plan,
            "status": GoalStatus.PROPOSED,
            "created_at": _iso(now),
            "confirmed_at": None,
            "current_step_id": None,
            "revision": 1,
        })
        for step in plan.steps:
            step.status = GoalStepStatus.PENDING
            # 规则从提案时刻开始生效：避免确认后到首次扫描之间的事件被漏掉。
            step.armed_at = _iso(now)
        task.task_type = GOAL_TASK_TYPE
        task.task_state = self._state_with_plan(task, plan)
        self.repo.transition_task(
            task, TaskStatus.BLOCKED_USER.value,
            event_type="goal_proposed",
            event_payload=summarize(plan),
            visibility="conversation",
        )
        task.wake_at = now + timedelta(hours=max(1, settings.aiive_goal_proposal_reminder_hours))
        self.repo.checkpoint(task.id, reason="goal_proposed", plan=plan.model_dump(mode="json"))
        return plan

    def confirm(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status != GoalStatus.PROPOSED:
            raise GoalError("goal_not_proposed")
        now = utcnow()
        plan.status = GoalStatus.ACTIVE
        plan.confirmed_at = _iso(now)
        plan.last_progress_at = _iso(now)
        plan.revision += 1
        task.wake_at = None
        self._save(task, plan)
        self.repo.append_event(
            task.id, "goal_confirmed", summarize(plan), visibility="conversation",
        )
        if task.status == TaskStatus.BLOCKED_USER.value:
            self.repo.transition_task(task, TaskStatus.QUEUED.value)
        self.schedule_pending_watches(task, plan)
        runnable = next_runnable_step(plan, now)
        if runnable is not None and self._needs_approval(runnable):
            # 首个可执行步骤需要审批：先停在用户侧，审批通过后才入队。
            runnable.status = GoalStepStatus.WAITING_USER
            plan.current_step_id = runnable.id
            self._save(task, plan)
            task.wake_at = now + timedelta(
                hours=max(1, settings.aiive_goal_wait_reminder_hours)
            )
            self.repo.transition_task(
                task, TaskStatus.BLOCKED_USER.value,
                event_type="goal_waiting",
                event_payload={"reason": "awaiting_approval", "step_id": runnable.id},
                visibility="conversation",
            )
            self._notify(
                task, title="步骤等待审批",
                content=f"「{runnable.title}」需要你确认后才会执行，可在任务中心审批。",
                category="goal_step_approval", dedupe=f"{runnable.id}:v{plan.revision}",
            )
        else:
            # 由 _advance 统一决定：可执行则入队，否则停在等待态由 watcher/用户唤醒。
            advanced = self._advance(task, plan)
            if advanced == "advance":
                self.repo.enqueue_run(task, trigger="goal_confirmed")
            else:
                self.repo.transition_task(
                    task, TaskStatus.BLOCKED_USER.value,
                    event_type="goal_waiting",
                    event_payload={"reason": "no_runnable_step"},
                    visibility="conversation",
                )
        self.repo.checkpoint(task.id, reason="goal_confirmed", plan=plan.model_dump(mode="json"))
        return self.detail(task)

    def reject(self, task_id: str, reason: str = "") -> dict[str, Any]:
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status not in {GoalStatus.PROPOSED, GoalStatus.PAUSED, GoalStatus.BLOCKED}:
            raise GoalError("goal_not_rejectable")
        plan.status = GoalStatus.REJECTED
        plan.blocked_reason = reason[:500]
        plan.revision += 1
        self._save(task, plan)
        self.cancel_watches(task)
        task.wake_at = None
        self.repo.append_event(
            task.id, "goal_rejected", {"reason": reason}, visibility="conversation",
        )
        self.repo.cancel_task(task.id, reason="goal_rejected")
        return self.detail(task)

    def pause(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status not in {GoalStatus.ACTIVE, GoalStatus.BLOCKED}:
            raise GoalError("goal_not_pausable")
        plan.status = GoalStatus.PAUSED
        plan.revision += 1
        self._save(task, plan)
        self.cancel_watches(task)
        task.wake_at = None
        self.repo.append_event(task.id, "goal_paused", {}, visibility="conversation")
        if task.status == TaskStatus.QUEUED.value:
            self.repo.transition_task(task, TaskStatus.BLOCKED_USER.value)
        self.repo.checkpoint(task.id, reason="goal_paused", plan=plan.model_dump(mode="json"))
        return self.detail(task)

    def resume(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status != GoalStatus.PAUSED:
            raise GoalError("goal_not_paused")
        plan.status = GoalStatus.ACTIVE
        plan.blocked_reason = ""
        plan.revision += 1
        self._save(task, plan)
        self.schedule_pending_watches(task, plan)
        self.repo.append_event(task.id, "goal_resumed", {}, visibility="conversation")
        if task.status == TaskStatus.BLOCKED_USER.value:
            self.repo.transition_task(task, TaskStatus.QUEUED.value)
            if self._advance(task, plan) == "advance":
                self.repo.enqueue_run(task, trigger="goal_resumed")
            else:
                self.repo.transition_task(task, TaskStatus.BLOCKED_USER.value)
        self.repo.checkpoint(task.id, reason="goal_resumed", plan=plan.model_dump(mode="json"))
        return self.detail(task)

    def end(self, task_id: str, reason: str = "") -> dict[str, Any]:
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        plan.status = GoalStatus.ABANDONED
        plan.blocked_reason = reason[:500]
        plan.revision += 1
        self._save(task, plan)
        self.cancel_watches(task)
        task.wake_at = None
        self.repo.append_event(
            task.id, "goal_ended", {"reason": reason}, visibility="conversation",
        )
        if task.status not in {
            TaskStatus.SUCCEEDED.value, TaskStatus.PARTIAL.value,
            TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
        }:
            self.repo.cancel_task(task.id, reason="goal_ended")
        return self.detail(task)

    # 执行内容字段：任一变化都意味着这一步要重做，旧状态/证据/审批不再可信。
    CONTENT_FIELDS: ClassVar[tuple[str, ...]] = ("title", "kind", "acceptance", "depends_on", "requires_evidence")

    def modify(self, task_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """修改计划：执行内容变化与调度时间变化分开处理。

        - 执行内容（任务说明/验收标准/依赖/执行模式）变化：步骤回到 pending，清空证据与审批，
          并递归失效依赖它的下游步骤，避免“依赖重做但下游仍算完成”。
        - 只改调度时间：已完成步骤保留完成状态与证据；未完成步骤重置触发时间后重新排期。
        """
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status in {GoalStatus.COMPLETED, GoalStatus.REJECTED, GoalStatus.ABANDONED}:
            raise GoalError("goal_terminal")
        if task.status in {
            TaskStatus.DISPATCHING.value, TaskStatus.RUNNING.value,
            TaskStatus.VERIFYING.value, TaskStatus.RECONCILING.value,
        }:
            # 运行中的步骤正在写证据，此时改计划会让结算与计划错位：先暂停再改。
            raise GoalError("goal_running")
        previous = {step.id: step for step in plan.steps}
        payload = plan.model_dump(mode="json")
        for key in ("summary", "deadline", "timezone", "acceptance", "steps"):
            if key in changes and changes[key] is not None:
                payload[key] = changes[key]
        candidate = GoalPlan.model_validate(payload)
        now = utcnow()

        invalidated: dict[str, str] = {}
        approval_invalidated: set[str] = set()
        rescheduled: list[str] = []
        added: list[str] = []
        for step in candidate.steps:
            old = previous.get(step.id)
            if old is None:
                added.append(step.id)
                continue
            if self._content_changed(old, step):
                invalidated[step.id] = "content_changed"
                if old.approved_at:
                    approval_invalidated.add(step.id)
                continue
            self._copy_step_state(step, old)
            if step.schedule != old.schedule or step.trigger != old.trigger:
                step.next_trigger_at = None
                rescheduled.append(step.id)
                if old.status not in FINISHED_STEP_STATES:
                    step.status = GoalStepStatus.PENDING

        # 依赖被重做的步骤，其下游结果同样失效（递归到不动点）。
        changed = True
        while changed:
            changed = False
            for step in candidate.steps:
                if step.id in invalidated or step.id in added:
                    continue
                for dep_id in step.depends_on:
                    if dep_id not in invalidated:
                        continue
                    invalidated[step.id] = f"dependency_changed:{dep_id}"
                    old = previous.get(step.id)
                    if old is not None and old.approved_at:
                        approval_invalidated.add(step.id)
                    changed = True
                    break

        for step_id, reason in invalidated.items():
            step = candidate.step(step_id)
            if step is not None:
                self._invalidate_step(step, reason, now)

        if candidate.current_step_id and candidate.current_step_id in invalidated:
            candidate.current_step_id = None
        candidate.revision = plan.revision + 1
        candidate.confirmed_at = plan.confirmed_at
        self.cancel_watches(task)
        self._save(task, candidate)
        self.schedule_pending_watches(task, candidate)
        self.repo.append_event(
            task.id, "goal_modified",
            {
                "revision": candidate.revision,
                "added": added,
                "rescheduled": rescheduled,
                "invalidated": [
                    {"step_id": step_id, "reason": reason}
                    for step_id, reason in invalidated.items()
                ],
                "approval_invalidated": sorted(approval_invalidated),
            },
            visibility="conversation",
        )
        if candidate.status == GoalStatus.ACTIVE:
            if task.status == TaskStatus.BLOCKED_USER.value:
                self.repo.transition_task(task, TaskStatus.QUEUED.value)
                if self._advance(task, candidate) == "advance":
                    self.repo.enqueue_run(task, trigger="goal_modified")
                else:
                    self.repo.transition_task(task, TaskStatus.BLOCKED_USER.value)
            elif task.status == TaskStatus.QUEUED.value:
                # 已有排队中的 run 会领取当前步骤：只同步计划状态，避免重复入队。
                self._advance(task, candidate)
        self.repo.checkpoint(task.id, reason="goal_modified", plan=candidate.model_dump(mode="json"))
        return self.detail(task)

    @classmethod
    def _content_changed(cls, old: GoalStep, new: GoalStep) -> bool:
        return any(getattr(old, field) != getattr(new, field) for field in cls.CONTENT_FIELDS)

    @staticmethod
    def _copy_step_state(step: GoalStep, old: GoalStep) -> None:
        step.status = old.status
        step.evidence_refs = list(old.evidence_refs)
        step.attempts = old.attempts
        step.missed_count = old.missed_count
        step.last_result = old.last_result
        step.last_occurrence = old.last_occurrence
        step.note = old.note
        step.approved_at = old.approved_at
        step.next_trigger_at = old.next_trigger_at
        # V50 触发状态：修改计划不能重置，否则同一规则会被重复触发并重复发消息。
        step.trigger_count = old.trigger_count
        step.last_trigger_key = old.last_trigger_key
        step.last_trigger_at = old.last_trigger_at
        step.armed_at = old.armed_at
        step.expired_at = old.expired_at
        step.cooldown_until = old.cooldown_until
        step.opportunistic_hits = list(old.opportunistic_hits)
        step.last_message_key = old.last_message_key

    @staticmethod
    def _invalidate_step(step: GoalStep, reason: str, now: datetime) -> None:
        """执行内容变化后：状态、证据、审批一并失效，旧结论不再被信任。"""
        step.status = GoalStepStatus.PENDING
        step.evidence_refs = []
        step.approved_at = None
        step.next_trigger_at = None
        step.last_result = None
        step.missed_count = 0
        step.note = reason[:2000]
        # 一次性计划若时间已过，重新排期算不出下一次触发，直接置为待执行，避免永久卡住。
        if step.schedule is not None and step.schedule.kind == "once":
            due = parse_datetime(step.schedule.at)
            if due is not None and due <= now and next_occurrence(step.schedule, now) is None:
                step.status = GoalStepStatus.READY

    def start_step(self, task_id: str, step_id: str) -> dict[str, Any]:
        """用户主动开始一个交互步骤（例如点击“开始模拟面试”）。"""
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status != GoalStatus.ACTIVE:
            raise GoalError("goal_not_active")
        step = plan.step(step_id)
        if step is None:
            raise GoalError("step_not_found")
        if step.status in {GoalStepStatus.DONE, GoalStepStatus.SKIPPED}:
            raise GoalError("step_already_finished")
        if not dependencies_met(plan, step):
            raise GoalError("step_dependencies_unmet")
        step.status = GoalStepStatus.READY
        plan.current_step_id = step.id
        self._save(task, plan)
        self.repo.append_event(
            task.id, "goal_step_started_by_user", {"step_id": step.id}, visibility="conversation",
        )
        if task.status == TaskStatus.BLOCKED_USER.value:
            self.repo.transition_task(task, TaskStatus.QUEUED.value)
            self.repo.enqueue_run(task, trigger="goal_step_start")
        return self.detail(task)

    def approve_step(self, task_id: str, step_id: str) -> dict[str, Any]:
        """审批 approval_required 步骤：通过后由运行时执行，无需用户再参与。"""
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status != GoalStatus.ACTIVE:
            raise GoalError("goal_not_active")
        step = plan.step(step_id)
        if step is None:
            raise GoalError("step_not_found")
        if step.kind != GoalStepKind.APPROVAL_REQUIRED.value:
            raise GoalError("step_not_approvable")
        if step.status in {GoalStepStatus.DONE, GoalStepStatus.SKIPPED}:
            raise GoalError("step_already_finished")
        if step.approved_at:
            raise GoalError("step_already_approved")
        if not dependencies_met(plan, step):
            raise GoalError("step_dependencies_unmet")
        step.approved_at = _iso(utcnow())
        step.status = GoalStepStatus.READY
        plan.current_step_id = step.id
        self._save(task, plan)
        self.repo.append_event(
            task.id, "goal_step_approved", {"step_id": step.id}, visibility="conversation",
        )
        self.repo.checkpoint(
            task.id, reason="goal_step_approved", plan=plan.model_dump(mode="json"),
        )
        if task.status == TaskStatus.BLOCKED_USER.value:
            self.repo.transition_task(task, TaskStatus.QUEUED.value)
            self.repo.enqueue_run(task, trigger="goal_step_approved")
        return self.detail(task)

    # ------------------------------------------------------------------
    # 触发决策钩子（由 goal_trigger_decision Handler 调用）
    # ------------------------------------------------------------------
    def save_plan(self, task: AgentTask, plan: GoalPlan) -> None:
        """公开的计划持久化入口：触发扫描与决策执行共用同一序列化。"""
        self._save(task, plan)

    def begin_step_from_trigger(self, task_id: str, step_id: str, *, trigger: str = "goal_trigger") -> dict[str, Any]:
        """LLM 决定执行某一步：服务端复核依赖/审批后再起跑，LLM 不能绕过。"""
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status != GoalStatus.ACTIVE:
            raise GoalError("goal_not_active")
        step = plan.step(step_id)
        if step is None:
            raise GoalError("step_not_found")
        if step.status in {GoalStepStatus.DONE, GoalStepStatus.SKIPPED}:
            raise GoalError("step_already_finished")
        if step.status == GoalStepStatus.RUNNING:
            raise GoalError("step_running")
        if not dependencies_met(plan, step):
            raise GoalError("step_dependencies_unmet")
        if self._needs_approval(step):
            # 需要人工审批的步骤只能停在用户侧，触发决策不得代替审批。
            step.status = GoalStepStatus.WAITING_USER
            plan.current_step_id = step.id
            self._save(task, plan)
            raise GoalError("step_requires_approval")
        now = utcnow()
        step.status = GoalStepStatus.READY
        plan.current_step_id = step.id
        plan.last_progress_at = _iso(now)
        self._save(task, plan)
        self.repo.append_event(
            task.id, "goal_step_triggered",
            {"step_id": step.id, "trigger": trigger},
            visibility="conversation",
        )
        if task.status in {
            TaskStatus.BLOCKED_USER.value, TaskStatus.BLOCKED_NODE.value,
            TaskStatus.BLOCKED_APPROVAL.value,
        }:
            self.repo.transition_task(task, TaskStatus.QUEUED.value)
        if task.status == TaskStatus.QUEUED.value:
            self.repo.enqueue_run(task, trigger=trigger)
        self.repo.checkpoint(task.id, reason="goal_step_triggered", plan=plan.model_dump(mode="json"))
        return self.detail(task)

    def mark_step_waiting_user(self, task_id: str, step_id: str, question: str) -> dict[str, Any]:
        """LLM 决定等待用户：只阻塞该步骤，其他独立步骤继续可执行。"""
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status != GoalStatus.ACTIVE:
            raise GoalError("goal_not_active")
        step = plan.step(step_id)
        if step is None:
            raise GoalError("step_not_found")
        if step.status in {GoalStepStatus.DONE, GoalStepStatus.SKIPPED}:
            raise GoalError("step_already_finished")
        now = utcnow()
        step.status = GoalStepStatus.WAITING_USER
        step.note = question[:500]
        plan.reminder.pop(f"wait_reminded:{step.id}", None)
        if plan.current_step_id == step.id:
            plan.current_step_id = None
        self._save(task, plan)
        task.wake_at = now + timedelta(hours=max(1, settings.aiive_goal_wait_reminder_hours))
        self.repo.append_event(
            task.id, "goal_step_waiting_user",
            {"step_id": step.id, "question": question},
            visibility="conversation",
        )
        return self.detail(task)

    def answer_step_question(
        self,
        task_id: str,
        step_id: str,
        *,
        answer: str,
        question_id: str = "",
        user_event_id: str = "",
        intent: str = "answer",
        plan_changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把用户回复关联到某一步：解除等待 / 修改计划 / 记录拒绝（服务端校验）。

        审批步骤不在本路径放行：即使问题来自审批步骤，也必须继续走原有审批机制。
        回答内容按既有输入语义进入 task_state.inputs，下一次 Run 能读到。
        """
        text = (answer or "").strip()
        if not text:
            raise GoalError("answer_required")
        if intent not in {"answer", "modify_plan", "reject"}:
            raise GoalError("intent_invalid")
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status != GoalStatus.ACTIVE:
            raise GoalError("goal_not_active")
        step = plan.step(step_id)
        if step is None:
            raise GoalError("step_not_found")
        if step.status in FINISHED_STEP_STATES:
            raise GoalError("step_already_finished")
        if self._needs_approval(step):
            # 审批边界：对话里的回答不能替代人工审批。
            raise GoalError("step_requires_approval")
        now = utcnow()
        if intent == "modify_plan":
            if not plan_changes:
                raise GoalError("plan_changes_invalid")
            self.modify(task.id, dict(plan_changes))
            return {"status": "applied", "action": "modify_plan", "step_id": step.id}
        if step.status != GoalStepStatus.WAITING_USER:
            raise GoalError("step_not_waiting_user")
        if intent == "reject":
            step.status = GoalStepStatus.SKIPPED
            step.note = f"用户拒绝：{text}"[:500]
            plan.reminder.pop(f"wait_reminded:{step.id}", None)
            if plan.current_step_id == step.id:
                plan.current_step_id = None
            plan.last_progress_at = _iso(now)
            self._save(task, plan)
            self.repo.append_event(
                task.id, "goal_step_answered",
                {
                    "step_id": step.id, "question_id": question_id,
                    "user_event_id": user_event_id, "intent": "reject",
                    "answer": text[:500], "result": "skipped",
                },
                visibility="conversation",
            )
            self.repo.checkpoint(
                task.id, reason="goal_question_rejected",
                plan=plan.model_dump(mode="json"),
            )
            return {"status": "applied", "action": "reject", "step_id": step.id}

        # answer：先按既有输入语义记录回答（BLOCKED_USER → QUEUED 并唤醒），
        # 再把步骤从 waiting_user 解除，让回答进入下一次 Run 的上下文。
        was_blocked = task.status == TaskStatus.BLOCKED_USER.value
        task = self.repo.send_input(task.id, text)
        plan = self._require_plan(task)
        step = plan.step(step_id)
        if step is None:
            raise GoalError("step_not_found")
        if step.status != GoalStepStatus.WAITING_USER:
            raise GoalError("step_not_waiting_user")
        step.status = GoalStepStatus.READY
        plan.current_step_id = step.id
        plan.last_progress_at = _iso(now)
        self._save(task, plan)
        self.repo.append_event(
            task.id, "goal_step_answered",
            {
                "step_id": step.id, "question_id": question_id,
                "user_event_id": user_event_id, "intent": "answer",
                "answer": text[:500], "result": "ready",
            },
            visibility="conversation",
        )
        # send_input 只在 BLOCKED_USER 时唤醒任务；其余状态需要补一次显式入队。
        if not was_blocked and task.status == TaskStatus.QUEUED.value:
            self.repo.enqueue_run(task, trigger="goal_question_answer")
        self.repo.checkpoint(
            task.id, reason="goal_question_answered", plan=plan.model_dump(mode="json"),
        )
        return {"status": "applied", "action": "answer", "step_id": step.id}

    def complete_goal(self, task_id: str) -> dict[str, Any]:
        """LLM 判定目标完成：服务端复核全部步骤与证据后才真正结束。"""
        task = self._require_task(task_id)
        plan = self._require_plan(task)
        if plan.status not in {GoalStatus.ACTIVE, GoalStatus.BLOCKED}:
            raise GoalError("goal_not_completable")
        if task.status not in {
            TaskStatus.BLOCKED_USER.value, TaskStatus.BLOCKED_NODE.value,
            TaskStatus.BLOCKED_APPROVAL.value, TaskStatus.QUEUED.value,
        }:
            # 运行中/已终结的任务不能由决策直接收敛到 SUCCEEDED（状态机会拒绝）。
            raise GoalError("goal_running")
        if not plan_complete(plan):
            raise GoalError("goal_not_complete")
        missing = [
            item.id for item in plan.steps
            if item.requires_evidence and not item.evidence_refs
        ]
        if missing:
            raise GoalError("goal_missing_evidence:" + ",".join(missing))
        now = utcnow()
        plan.status = GoalStatus.COMPLETED
        plan.current_step_id = None
        plan.last_progress_at = _iso(now)
        self._save(task, plan)
        self.cancel_watches(task)
        task.wake_at = None
        self.repo.append_event(
            task.id, "goal_completed", summarize(plan), visibility="conversation",
        )
        report = {
            "status": "succeeded",
            "summary": plan.summary or task.title,
            "progress": plan.progress(),
        }
        # 任务状态机不允许 QUEUED/BLOCKED 直接跳到 SUCCEEDED，沿既有路径收敛到 VERIFYING。
        if task.status in {
            TaskStatus.BLOCKED_USER.value, TaskStatus.BLOCKED_NODE.value,
            TaskStatus.BLOCKED_APPROVAL.value, TaskStatus.QUEUED.value,
        }:
            if task.status != TaskStatus.QUEUED.value:
                self.repo.transition_task(task, TaskStatus.QUEUED.value)
            self.repo.transition_task(task, TaskStatus.DISPATCHING.value)
            self.repo.transition_task(task, TaskStatus.RUNNING.value)
        self.repo.transition_task(task, TaskStatus.VERIFYING.value)
        self.repo.transition_task(
            task, TaskStatus.SUCCEEDED.value, report=report,
            event_type="task_completed", event_payload=report,
            visibility="conversation",
        )
        self.repo.checkpoint(task.id, reason="goal_completed", plan=plan.model_dump(mode="json"))
        return self.detail(task)

    # ------------------------------------------------------------------
    # 执行钩子（由 PersistentTaskRuntime 调用）
    # ------------------------------------------------------------------
    def on_run_started(self, task: AgentTask, run: AgentRun) -> bool:
        plan = self.load_plan(task)
        if plan is None or plan.status != GoalStatus.ACTIVE:
            return False
        now = utcnow()
        step = plan.current_step()
        if (
            step is None
            or step.status not in {GoalStepStatus.READY, GoalStepStatus.RUNNING}
            # 防御：任何触发路径都不能让依赖未满足的步骤抢先执行。
            or not dependencies_met(plan, step)
        ):
            step = next_runnable_step(plan, now)
        if step is None:
            return False
        if self._needs_approval(step):
            # 防御：任何触发路径都不得绕过审批启动步骤。
            return False
        step.status = GoalStepStatus.RUNNING
        step.attempts += 1
        plan.current_step_id = step.id
        plan.last_progress_at = _iso(now)
        self._save(task, plan)
        self.repo.append_event(
            task.id, "goal_step_running",
            {"step_id": step.id, "title": step.title, "attempt": step.attempts},
            visibility="task", run_id=run.id,
        )
        return True

    def on_step_finished(self, task: AgentTask, run: AgentRun, report: dict[str, Any]) -> str:
        """一次 Run 结束后的步骤结算：advance / wait / complete。"""
        plan = self.load_plan(task)
        if plan is None:
            return "complete"
        step = plan.current_step()
        now = utcnow()
        status = str(report.get("status") or "")
        evidence = [str(item) for item in (report.get("evidence_refs") or []) if item]
        if step is not None:
            step.evidence_refs = sorted({*step.evidence_refs, *evidence})
            if status == "succeeded" and step.requires_evidence and not step.evidence_refs:
                # 不允许仅凭 LLM 宣称成功就推进目标。
                step.status = GoalStepStatus.FAILED
                step.note = "missing_evidence"
                step.last_result = {"status": "failed", "reason": "missing_evidence", "at": _iso(now)}
                plan.status = GoalStatus.BLOCKED
                plan.blocked_reason = f"step_without_evidence:{step.id}"
                plan.current_step_id = None
                self._save(task, plan)
                self.repo.append_event(
                    task.id, "goal_step_failed",
                    {"step_id": step.id, "reason": "missing_evidence"},
                    visibility="conversation", run_id=run.id,
                )
                self._notify(
                    task, title="目标步骤缺少证据",
                    content=f"「{step.title}」没有可验证的证据，已暂停推进，请确认结果或补充验证。",
                    category="goal_missing_evidence", dedupe=f"{step.id}:{step.attempts}",
                )
                return "wait"
            if status == "succeeded":
                step.last_result = {"status": "succeeded", "summary": report.get("summary", ""), "at": _iso(now)}
                nxt = next_occurrence(step.schedule, now) if step.schedule is not None else None
                if nxt is not None and step.kind != GoalStepKind.AUTONOMOUS.value:
                    step.status = GoalStepStatus.WAITING_TIME
                    step.next_trigger_at = _iso(nxt)
                    step.note = "等待下一次周期"
                    plan.current_step_id = None
                    self._save(task, plan)
                    self.repo.append_event(
                        task.id, "goal_step_completed",
                        {"step_id": step.id, "next_trigger_at": step.next_trigger_at},
                        visibility="conversation", run_id=run.id,
                    )
                    self.schedule_pending_watches(task, plan)
                    return "wait"
                step.status = GoalStepStatus.DONE
                step.note = ""
            elif status == "partial":
                step.status = GoalStepStatus.FAILED
                step.last_result = {"status": "partial", "summary": report.get("summary", ""), "at": _iso(now)}
                step.note = str(report.get("summary") or "partial")[:500]
            else:
                step.status = GoalStepStatus.FAILED
                step.last_result = {"status": "failed", "summary": report.get("summary", ""), "at": _iso(now)}
                step.note = str(report.get("summary") or "failed")[:500]
            plan.current_step_id = None
            self.repo.append_event(
                task.id, f"goal_step_{step.status}",
                {"step_id": step.id, "status": step.status, "progress": plan.progress()},
                visibility="conversation", run_id=run.id,
            )
        return self._advance(task, plan)

    def on_step_awaiting_user(self, task: AgentTask, run: AgentRun, question: str) -> str:
        """某一步等待用户输入：只阻塞依赖它的步骤，其他独立步骤继续。"""
        plan = self.load_plan(task)
        if plan is None:
            return "wait"
        step = plan.current_step()
        now = utcnow()
        if step is not None:
            step.status = GoalStepStatus.WAITING_USER
            step.note = question[:500]
            plan.reminder.pop(f"wait_reminded:{step.id}", None)
            plan.current_step_id = None
            self._save(task, plan)
            self.repo.append_event(
                task.id, "goal_step_waiting_user",
                {"step_id": step.id, "question": question},
                visibility="conversation", run_id=run.id,
            )
        following = next_runnable_step(plan, now)
        if following is not None:
            plan.current_step_id = following.id
            following.status = GoalStepStatus.READY
            self._save(task, plan)
            return "advance"
        task.wake_at = now + timedelta(hours=max(1, settings.aiive_goal_wait_reminder_hours))
        return "wait"

    def on_goal_blocked(self, task: AgentTask, reason: str) -> None:
        plan = self.load_plan(task)
        if plan is None or plan.status != GoalStatus.ACTIVE:
            return
        plan.status = GoalStatus.BLOCKED
        plan.blocked_reason = reason[:500]
        self._save(task, plan)
        self.repo.append_event(
            task.id, "goal_blocked", {"reason": reason}, visibility="conversation",
        )

    def is_paused(self, task: AgentTask) -> bool:
        plan = self.load_plan(task)
        return plan is not None and plan.status == GoalStatus.PAUSED

    # ------------------------------------------------------------------
    # 定时触发
    # ------------------------------------------------------------------
    def schedule_pending_watches(self, task: AgentTask, plan: GoalPlan) -> int:
        now = utcnow()
        created = 0
        for step in plan.steps:
            # 显式触发规则（V50）由 GoalTriggerService 驱动，不再建 time watch，
            # 否则同一规则会出现"watch 到点 + 扫描器命中"两条启动路径。
            if step.schedule is None or step.trigger is not None:
                continue
            # WAITING_USER 也要排下一次：周期交互步骤本次已通知，但下一场仍需触发。
            if step.status not in {
                GoalStepStatus.PENDING, GoalStepStatus.WAITING_TIME, GoalStepStatus.WAITING_USER,
            }:
                continue
            due = parse_datetime(step.next_trigger_at) or next_occurrence(step.schedule, now)
            if due is None:
                continue
            step.next_trigger_at = _iso(due)
            if self._ensure_watch(task, step, due):
                created += 1
        self._save(task, plan)
        return created

    def _ensure_watch(self, task: AgentTask, step: GoalStep, due: datetime) -> bool:
        occurrence = _iso(due)
        existing = self.db.query(AgentTaskWatch).filter(
            AgentTaskWatch.task_id == task.id,
            AgentTaskWatch.status == "active",
        ).all()
        for watch in existing:
            config = watch.config or {}
            if config.get("goal_step_id") == step.id and config.get("occurrence") == occurrence:
                return False
        watch = AgentTaskWatch(
            task_id=task.id,
            watch_type="time",
            config={
                "at": occurrence,
                "goal_step_id": step.id,
                "occurrence": occurrence,
                "notify_only": step.kind != GoalStepKind.AUTONOMOUS.value,
                "interval_seconds": 30,
            },
            cursor={},
            status="active",
            next_check_at=due,
        )
        self.db.add(watch)
        self.db.flush()
        return True

    def cancel_watches(self, task: AgentTask) -> int:
        count = self.db.query(AgentTaskWatch).filter(
            AgentTaskWatch.task_id == task.id,
            AgentTaskWatch.status == "active",
        ).update({AgentTaskWatch.status: "paused"}, synchronize_session=False)
        self.db.flush()
        return int(count or 0)

    def on_watch_due(self, task: AgentTask, watch: AgentTaskWatch) -> None:
        """Watcher 到期回调：结算上一场是否错过，并安排下一次触发。"""
        config = watch.config or {}
        step_id = str(config.get("goal_step_id") or "")
        occurrence = str(config.get("occurrence") or "")
        plan = self.load_plan(task)
        if plan is None or step_id == "" or plan.status != GoalStatus.ACTIVE:
            return
        step = plan.step(step_id)
        if step is None:
            return
        now = utcnow()
        if step.status == GoalStepStatus.WAITING_USER:
            # 上一场到期后用户始终没开始：记 missed（不视为完成），也不重复启动同一场。
            step.status = GoalStepStatus.MISSED
            step.missed_count += 1
            plan.missed_streak += 1
            self._save(task, plan)
            self.repo.append_event(
                task.id, "goal_step_missed",
                {
                    "step_id": step.id,
                    "occurrence": step.last_occurrence or occurrence,
                    "missed_count": step.missed_count,
                },
                visibility="conversation",
            )
            if step.missed_count >= max(1, settings.aiive_goal_missed_streak_threshold):
                self._notify(
                    task, title="目标节奏需要调整",
                    content=f"「{step.title}」已连续错过 {step.missed_count} 次，是否调整时间或暂停计划？",
                    category="goal_missed_streak",
                    dedupe=f"{step.id}:{step.missed_count}",
                )
                self.repo.append_event(
                    task.id, "goal_adjustment_suggested",
                    {"step_id": step.id, "missed_count": step.missed_count},
                    visibility="conversation",
                )
        step.last_occurrence = occurrence
        step.next_trigger_at = None
        if step.kind == GoalStepKind.AUTONOMOUS.value:
            step.status = GoalStepStatus.READY
            if plan.current_step() is None:
                plan.current_step_id = step.id
        elif self._needs_approval(step):
            step.status = GoalStepStatus.WAITING_USER
            self._notify(
                task, title="步骤等待审批",
                content=f"「{step.title}」已到计划时间，需要你审批后才会执行。",
                category="goal_step_approval",
                dedupe=f"{step.id}:{occurrence}",
            )
        else:
            step.status = GoalStepStatus.WAITING_USER
            self._notify(
                task, title="到了计划时间",
                content=f"「{step.title}」已到期，准备好后点击开始（不会自动打断当前对话）。",
                category="goal_step_due",
                dedupe=f"{step.id}:{occurrence}",
            )
        nxt = next_occurrence(step.schedule, now)
        if nxt is not None:
            step.next_trigger_at = _iso(nxt)
        self._save(task, plan)
        self.repo.append_event(
            task.id, "goal_step_due",
            {"step_id": step.id, "occurrence": occurrence, "next_trigger_at": step.next_trigger_at},
            visibility="conversation",
        )
        self.schedule_pending_watches(task, plan)

    # ------------------------------------------------------------------
    # 沉默策略（确定性扫描，不调用 LLM）
    # ------------------------------------------------------------------
    def scan_due_reminders(self, *, now: datetime | None = None, limit: int = 50) -> int:
        now = now or utcnow()
        tasks = self.db.query(AgentTask).filter(
            AgentTask.task_type == GOAL_TASK_TYPE,
            AgentTask.status == TaskStatus.BLOCKED_USER.value,
            AgentTask.wake_at.is_not(None),
            AgentTask.wake_at <= now,
        ).order_by(AgentTask.wake_at.asc()).limit(limit).all()
        handled = 0
        for task in tasks:
            plan = self.load_plan(task)
            if plan is None:
                task.wake_at = None
                handled += 1
                continue
            if plan.status == GoalStatus.PROPOSED:
                self._remind_proposal(task, plan, now)
            elif plan.status == GoalStatus.ACTIVE:
                self._remind_waiting_steps(task, plan, now)
            else:
                task.wake_at = None
            handled += 1
        self.db.flush()
        return handled

    def _remind_proposal(self, task: AgentTask, plan: GoalPlan, now: datetime) -> None:
        created = parse_datetime(plan.created_at) or now
        expire_at = created + timedelta(days=max(1, settings.aiive_goal_proposal_expire_days))
        if now >= expire_at:
            # 7 天仍无回复：保留草稿，停止催促。
            task.wake_at = None
            plan.reminder["proposal_expired_at"] = _iso(now)
            self._save(task, plan)
            self.repo.append_event(task.id, "goal_proposal_dormant", {}, visibility="task")
            return
        if not plan.reminder.get("proposal_reminded_at"):
            plan.reminder["proposal_reminded_at"] = _iso(now)
            self._save(task, plan)
            self._notify(
                task, title="目标计划待确认",
                content=f"「{task.title}」的计划还在等待你确认，确认后才会开始执行。",
                category="goal_proposal_pending", dedupe="proposal",
            )
            self.repo.append_event(
                task.id, "goal_proposal_reminded", {}, visibility="conversation",
            )
        task.wake_at = expire_at

    def _remind_waiting_steps(self, task: AgentTask, plan: GoalPlan, now: datetime) -> None:
        pending: list[GoalStep] = []
        for step in plan.steps:
            if step.status != GoalStepStatus.WAITING_USER:
                continue
            if plan.reminder.get(f"wait_reminded:{step.id}"):
                continue
            pending.append(step)
        if not pending:
            task.wake_at = None
            return
        step = pending[0]
        plan.reminder[f"wait_reminded:{step.id}"] = _iso(now)
        self._save(task, plan)
        self._notify(
            task, title="目标步骤在等待你",
            content=f"「{step.title}」需要你的输入才能继续：{step.note or '请回复或点击继续'}",
            category="goal_step_waiting_user", dedupe=step.id,
        )
        self.repo.append_event(
            task.id, "goal_step_reminded", {"step_id": step.id}, visibility="conversation",
        )
        task.wake_at = None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _advance(self, task: AgentTask, plan: GoalPlan) -> str:
        now = utcnow()
        if plan.status not in {GoalStatus.ACTIVE, GoalStatus.BLOCKED}:
            return "wait"
        step = next_runnable_step(plan, now)
        if step is not None and self._needs_approval(step):
            # 审批步骤先停在用户侧：没有人工审批，运行时不得启动。
            step.status = GoalStepStatus.WAITING_USER
            plan.current_step_id = step.id
            plan.status = GoalStatus.ACTIVE
            plan.blocked_reason = ""
            self._save(task, plan)
            task.wake_at = now + timedelta(
                hours=max(1, settings.aiive_goal_wait_reminder_hours)
            )
            self._notify(
                task, title="步骤等待审批",
                content=f"「{step.title}」需要你确认后才会执行，可在任务中心审批。",
                category="goal_step_approval", dedupe=f"{step.id}:v{plan.revision}",
            )
            return "wait"
        if step is not None:
            step.status = GoalStepStatus.READY
            plan.current_step_id = step.id
            plan.status = GoalStatus.ACTIVE
            plan.blocked_reason = ""
            self._save(task, plan)
            return "advance"
        # 只有旧版 schedule 步骤需要 wake_at 定时唤醒；显式触发规则由扫描器轮询。
        upcoming = [item for item in upcoming_steps(plan) if item.trigger is None]
        if upcoming:
            plan.current_step_id = None
            plan.last_progress_at = _iso(now)
            self._save(task, plan)
            self.schedule_pending_watches(task, plan)
            task.wake_at = parse_datetime(upcoming[0].next_trigger_at)
            return "wait"
        interactive = [
            item for item in plan.steps
            if item.kind == GoalStepKind.INTERACTIVE.value
            and item.status == GoalStepStatus.PENDING.value
            and item.schedule is None
            and item.trigger is None
            and dependencies_met(plan, item)
        ]
        if interactive:
            # 交互步骤不自动起跑：只提示用户可以开始，并等待其点击。
            for item in interactive:
                item.status = GoalStepStatus.WAITING_USER
            plan.current_step_id = None
            plan.last_progress_at = _iso(now)
            self._save(task, plan)
            first = interactive[0]
            self._notify(
                task, title="目标步骤等待你开始",
                content=f"「{first.title}」需要你参与，准备好后点击开始（不会自动打断当前对话）。",
                category="goal_step_ready", dedupe=f"{first.id}:v{plan.revision}",
            )
            task.wake_at = task.wake_at or (
                now + timedelta(hours=max(1, settings.aiive_goal_wait_reminder_hours))
            )
            return "wait"
        triggered = [
            item for item in plan.steps
            if trigger_of(item) is not None
            and item.status not in FINISHED_STEP_STATES
        ]
        if triggered:
            # 还有等待规则触发的步骤：保持 active，由触发扫描器唤醒，不误判为受阻。
            plan.current_step_id = None
            plan.last_progress_at = _iso(now)
            self._save(task, plan)
            task.wake_at = None
            return "wait"
        waiting = [item for item in plan.steps if item.status == GoalStepStatus.WAITING_USER]
        if waiting:
            # 有步骤在等用户输入：目标保持 active，其他步骤已完成也不误判为受阻。
            plan.current_step_id = None
            plan.last_progress_at = _iso(now)
            self._save(task, plan)
            task.wake_at = task.wake_at or (now + timedelta(hours=max(1, settings.aiive_goal_wait_reminder_hours)))
            return "wait"
        if plan_complete(plan):
            missing = [item.id for item in plan.steps if item.requires_evidence and not item.evidence_refs]
            if missing:
                plan.status = GoalStatus.BLOCKED
                plan.blocked_reason = "acceptance_without_evidence:" + ",".join(missing)
                self._save(task, plan)
                self._notify(
                    task, title="目标还不能算完成",
                    content="部分步骤没有可验证的证据，请确认结果后再结束目标。",
                    category="goal_missing_evidence", dedupe="acceptance",
                )
                return "wait"
            plan.status = GoalStatus.COMPLETED
            plan.current_step_id = None
            plan.last_progress_at = _iso(now)
            self._save(task, plan)
            return "complete"
        plan.status = GoalStatus.BLOCKED
        plan.blocked_reason = "steps_blocked"
        self._save(task, plan)
        self._notify(
            task, title="目标推进受阻",
            content="剩余步骤的依赖未满足或已失败，请查看任务中心后调整计划。",
            category="goal_blocked", dedupe=f"v{plan.revision}",
        )
        return "wait"

    @staticmethod
    def _needs_approval(step: GoalStep) -> bool:
        return (
            step.kind == GoalStepKind.APPROVAL_REQUIRED.value
            and not step.approved_at
        )

    def _save(self, task: AgentTask, plan: GoalPlan) -> None:
        task.task_state = self._state_with_plan(task, plan)
        self.db.flush()

    @staticmethod
    def _state_with_plan(task: AgentTask, plan: GoalPlan) -> dict[str, Any]:
        state = dict(task.task_state or {})
        state["goal"] = plan.model_dump(mode="json")
        state["plan"] = [
            {"id": step.id, "title": step.title, "kind": step.kind, "status": step.status}
            for step in plan.steps
        ]
        return state

    def _require_task(self, task_id: str) -> AgentTask:
        task = self.repo.get_task(task_id, for_update=True)
        if task is None:
            raise GoalError("task_not_found")
        if not self.is_goal_task(task):
            raise GoalError("goal_not_found")
        return task

    def _require_plan(self, task: AgentTask) -> GoalPlan:
        plan = self.load_plan(task)
        if plan is None:
            raise GoalError("goal_not_found")
        return plan

    def _notify(self, task: AgentTask, *, title: str, content: str, category: str, dedupe: str) -> str:
        """写一条待处理通知（复用通知读模型与角标广播），同一 key 只保留一条。"""
        key = f"goal:{category}:{dedupe}"
        existing = self.db.query(Event).filter(
            Event.trace_id == task.id,
            Event.event_type == "notification_created",
        ).order_by(Event.created_at.desc()).limit(20).all()
        for event in existing:
            payload = dict(event.payload or {})
            if payload.get("goal_key") == key and payload.get("status") == "pending":
                return event.id
        if self.db.get(Thread, task.thread_id) is None:
            return ""
        event = Event(
            id=str(uuid.uuid4()),
            trace_id=task.id,
            thread_id=task.thread_id,
            event_type="notification_created",
            payload={
                "title": title,
                "content": content,
                "message": content,
                "status": "pending",
                "category": category,
                "goal_key": key,
                "task_id": task.id,
            },
        )
        self.db.add(event)
        self.db.flush()
        try:
            from aiive.api.routes_notifications import count_pending_notifications
            from aiive.api.ws_manager import ws_manager

            ws_manager.broadcast_notification_sync(count_pending_notifications(self.db))
        except Exception:
            logger.warning("目标通知角标广播失败（不影响通知落库）", exc_info=True)
        return event.id
