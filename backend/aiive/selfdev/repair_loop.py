"""自主修复闭环：理解目标 → 读取代码 → 规划 → 应用补丁 → 定向测试 → 有限修复 → 安全门 → 受控晋升。

设计约束：
- 所有代码修改只进入**非活跃槽位**（``PatchExecutor``），绝不直接改活跃版本或用户数据；
- 复用 ``TargetedTestRunner`` / ``eval.gate`` / ``ReleaseManager``，不绕过任何安全门；
- 修复轮次有上限，补丁指纹相同即停止；无法可靠修复时明确失败，不伪造成功；
- 全过程写入结构化记录，可落库到 ``SelfDevRequest.plan["repair"]``。

晋升后的进程切换、健康检查与失败回滚由现有 Supervisor / ReleaseManager 负责，
本模块只负责提交晋升请求并如实记录结果。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from aiive.db.models import PatchOperation, SelfDevRequest
from aiive.selfdev.patch_executor import PatchExecutor
from aiive.selfdev.planner import SelfDevPlanner
from aiive.selfdev.project_reader import ProjectContext, ProjectReader
from aiive.selfdev.repair_guard import (
    Baseline,
    CandidateSlotGuard,
    RepairJournal,
    RepairLock,
    default_repair_lock,
    default_state_dir,
    recover_interrupted,
)
from aiive.selfdev.targeted_test_runner import TargetedTestRunner
from aiive.supervisor.slot_manager import tree_fingerprint

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROUNDS = 3
MAX_ROUNDS_CAP = 5
MAX_FEEDBACK_CHARS = 8_000

# 只有这些状态才算"闭环成功"；promote_requested 仍需 Supervisor 后续确认。
_SUCCESS_STATUSES: frozenset[str] = frozenset({
    "tests_passed", "promote_requested", "promoted",
})


def _now() -> str:
    """返回 ISO-8601 UTC 时间字符串（可直接 JSON 序列化）。"""
    return datetime.now(UTC).isoformat()


def _as_int(value: Any) -> int:
    """把任意值安全转换为 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _count(value: Any) -> int:
    """统计列表长度；非列表返回 0。"""
    return len(value) if isinstance(value, list) else 0


def patch_fingerprint(operations: list[Any]) -> str:
    """计算补丁的确定性指纹（操作类型 + 目标路径 + 内容哈希）。

    指纹相同意味着模型提交了完全相同的补丁；继续重试只会得到相同结果，
    因此修复循环必须在此停止，而不是耗尽轮次。
    """
    material: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        content = operation.get("content")
        text = content if isinstance(content, str) else ""
        material.append({
            "operation": str(operation.get("operation") or ""),
            "target_file": str(operation.get("target_file") or ""),
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "not_allowed_yet": bool(operation.get("not_allowed_yet")),
        })
    material.sort(key=lambda item: (str(item["target_file"]), str(item["operation"])))
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _summarize_operations(operations: list[Any]) -> list[dict[str, Any]]:
    """生成可落库的操作摘要（不含完整内容，避免记录膨胀）。"""
    summary: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        content = operation.get("content")
        text = content if isinstance(content, str) else ""
        encoded = text.encode("utf-8")
        summary.append({
            "operation": str(operation.get("operation") or ""),
            "target_file": str(operation.get("target_file") or ""),
            "content_sha256": hashlib.sha256(encoded).hexdigest() if encoded else "",
            "content_bytes": len(encoded),
            "not_allowed_yet": bool(operation.get("not_allowed_yet")),
            "reason": str(operation.get("reason") or ""),
            "risk_notes": str(operation.get("risk_notes") or ""),
            "requires_schema_change": bool(operation.get("requires_schema_change")),
            "safe_delete_scope": operation.get("safe_delete_scope"),
        })
    return summary


def _summarize_tests(report: dict[str, Any]) -> dict[str, Any]:
    """生成可落库的测试摘要（保留失败输出的尾部，供追溯）。"""
    items: list[dict[str, Any]] = []
    results = report.get("results")
    if isinstance(results, list):
        for item in results[:20]:
            if not isinstance(item, dict):
                continue
            items.append({
                "file": str(item.get("file") or ""),
                "result": str(item.get("result") or ""),
                "reason": str(item.get("reason") or ""),
                "output_tail": str(
                    item.get("stdout") or item.get("error") or ""
                )[-800:],
            })
    payload: dict[str, Any] = {
        "ok": bool(report.get("ok")),
        "total": _as_int(report.get("total")),
        "passed": _as_int(report.get("passed")),
        "failed": _as_int(report.get("failed")),
        "results": items,
    }
    error = report.get("error") or report.get("reason")
    if error:
        payload["error"] = str(error)
    return payload


def _extract_failure_feedback(report: dict[str, Any]) -> str:
    """从真实测试报告中提取失败输出，作为下一轮修复的输入。"""
    chunks: list[str] = []
    results = report.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            if item.get("result") == "passed":
                continue
            text = str(
                item.get("stdout") or item.get("error") or item.get("reason") or ""
            )
            chunks.append(
                f"### {item.get('file') or '?'} "
                + f"({item.get('result') or 'failed'})\n{text[-4000:]}"
            )
    if not chunks:
        fallback = report.get("error") or report.get("reason") or "targeted tests failed"
        return str(fallback)[-MAX_FEEDBACK_CHARS:]
    return "\n\n".join(chunks)[-MAX_FEEDBACK_CHARS:]


def _apply_failure_reason(result: dict[str, Any]) -> str:
    """从补丁应用结果中提取可读的失败原因。"""
    failed = result.get("operations_failed")
    if isinstance(failed, list) and failed:
        first = failed[0]
        if isinstance(first, dict):
            return (
                "apply_failed: "
                + str(first.get("reason") or first.get("error") or "unknown")
            )
    return str(result.get("error") or "apply_failed")


def _gate_summary(result: Any) -> Any:
    """把闸门结果规范化为可 JSON 化的摘要。"""
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    to_summary = getattr(result, "to_summary", None)
    if callable(to_summary):
        return to_summary()
    return {"ok": bool(getattr(result, "ok", False))}


def _gate_ok(result: Any) -> bool:
    """判断闸门是否放行。"""
    if isinstance(result, dict):
        return bool(result.get("ok"))
    return bool(getattr(result, "ok", False))


def _gate_error(result: Any) -> str:
    """提取闸门失败原因。"""
    if isinstance(result, dict):
        return str(result.get("error") or "safety_gate_failed")
    return str(getattr(result, "error", "") or "safety_gate_failed")


def _default_gate_runner(candidate_root: str) -> Any:
    """默认闸门：对候选槽位跑内核契约 + Replay Eval + 候选子进程评测。"""
    from aiive.eval.gate import evaluate_promotion_gate

    return evaluate_promotion_gate(Path(candidate_root).parent)


def _default_promote_runner() -> Any:
    """默认晋升：走生产入口 ReleaseManager（健康检查 + 闸门 + Supervisor）。"""
    from aiive.supervisor.client import supervisor_client_from_settings
    from aiive.supervisor.release_manager import ReleaseManager

    return ReleaseManager(client=supervisor_client_from_settings()).promote()


@dataclass
class RepairRound:
    """一次"应用补丁 + 定向测试"的完整记录。"""

    round_index: int
    patch_fingerprint: str
    changed_files: list[str]
    applied: int
    failed: int
    test_ok: bool
    test_summary: dict[str, Any]
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "round": self.round_index,
            "patch_fingerprint": self.patch_fingerprint,
            "changed_files": list(self.changed_files),
            "applied": self.applied,
            "failed": self.failed,
            "test_ok": self.test_ok,
            "test_summary": self.test_summary,
            "failure_reason": self.failure_reason,
        }


class SelfDevRepairLoop:
    """自主修复闭环编排器（依赖可注入，便于离线测试）。"""

    def __init__(
        self,
        *,
        llm_client: Any = None,
        planner: Any = None,
        reader: ProjectReader | None = None,
        executor: Any = None,
        test_runner_factory: Callable[[str], Any] | None = None,
        gate_runner: Callable[[str], Any] | None = None,
        promote_runner: Callable[[], Any] | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        guard: CandidateSlotGuard | None = None,
        lock: RepairLock | None = None,
        journal: RepairJournal | None = None,
    ) -> None:
        if planner is None:
            from aiive.core.llm_client import default_llm_client

            planner = SelfDevPlanner(llm_client or default_llm_client())
        self._planner: Any = planner
        self._reader: ProjectReader = reader or ProjectReader()
        self._executor: Any = executor or PatchExecutor()
        # 候选槽位守卫默认复用执行器持有的槽位管理器：应用补丁、回滚、
        # 标记必须看到同一组 A/B 槽位，否则隔离形同虚设。
        self._guard: CandidateSlotGuard = guard or CandidateSlotGuard(
            manager=getattr(self._executor, "_manager", None)
        )
        # 锁与运行日志跟随槽位管理器：生产落在仓库 runtime/selfdev，
        # 自定义槽位目录时随槽位目录走，避免不同部署互相干扰。
        state_dir = default_state_dir(self._guard.manager)
        self._lock: RepairLock = lock or default_repair_lock(self._guard.manager)
        self._journal: RepairJournal = journal or RepairJournal(state_dir / "runs")
        self._test_runner_factory: Callable[[str], Any] = (
            test_runner_factory
            or (lambda candidate_root: TargetedTestRunner(
                repo_root=Path(candidate_root)
            ))
        )
        self._gate_runner: Callable[[str], Any] = gate_runner or _default_gate_runner
        self._promote_runner: Callable[[], Any] = (
            promote_runner or _default_promote_runner
        )
        self._max_rounds: int = max(0, min(int(max_rounds), MAX_ROUNDS_CAP))

    # ── 主流程（并发控制 + 中断恢复） ─────────────────────────────

    def run(
        self,
        goal: str,
        *,
        trace_id: str | None = None,
        files: list[str] | None = None,
        promote: bool = False,
        run_gate: bool = True,
    ) -> dict[str, Any]:
        """执行一次完整的自主修复闭环，返回可审计的结构化记录。

        并发语义：同一项目同一时间只允许一个修复流程改写候选槽位。重复请求
        直接返回 repair_busy（ok=False，附当前持有者信息），不排队、不覆盖、
        不等待——调用方可以稍后重试。

        中断语义：进入闭环前先扫描上一次未完成的运行并按日志恢复候选槽位；
        本轮在任意阶段被强杀后，下一轮同样能识别并恢复。中断的运行只会被
        标记为 interrupted*，绝不会被记为成功。
        """
        record = self._new_record(goal, trace_id)
        run_id = str(record["run_id"])
        handle = self._lock.acquire(run_id=run_id)
        if handle is None:
            record["lock"] = {
                "acquired": False,
                "holder": self._lock.read_holder(),
            }
            return self._finish(
                record, "repair_busy", "concurrent_repair_in_progress",
            )
        record["lock"] = {"acquired": True, **handle.to_dict()}
        try:
            baseline = self._guard.capture_baseline()
            record["candidate"]["baseline"] = (
                baseline.to_dict() if baseline else None
            )
            record["candidate"]["baseline_available"] = baseline is not None
            # 基线必须先落日志：之后任何阶段中断都能据此回滚候选槽位。
            self._write_journal(record, phase="locked")
            record["recovery"] = recover_interrupted(
                self._journal, self._guard, exclude_run_id=run_id,
            )
            return self._run_locked(
                record, goal, trace_id, files, baseline=baseline,
                promote=promote, run_gate=run_gate,
            )
        except Exception as exc:
            logger.exception("修复闭环异常终止")
            self._restore_candidate(record, reason=f"unexpected_error: {exc}")
            return self._finish(record, "failed", f"unexpected_error: {exc}")
        finally:
            record["lock"]["released"] = self._lock.release(handle)

    def _run_locked(
        self,
        record: dict[str, Any],
        goal: str,
        trace_id: str | None,
        files: list[str] | None,
        *,
        baseline: Baseline | None,
        promote: bool,
        run_gate: bool,
    ) -> dict[str, Any]:
        """持锁执行闭环主流程（调用方保证已持有并发锁并记录基线）。"""
        context = self._collect_context(goal, files, record)
        plan: dict[str, Any] | None = None
        feedback: str | None = None
        previous_fingerprint = ""
        attempts = self._max_rounds + 1

        for attempt in range(attempts):
            plan = self._safe_plan(
                goal,
                trace_id,
                context,
                plan if attempt > 0 else None,
                feedback,
            )
            if plan is None:
                return self._finish(
                    record, "planner_failed",
                    "planner_failed_or_returned_invalid_plan",
                )

            raw_operations = plan.get("operations")
            operations = (
                [item for item in raw_operations if isinstance(item, dict)]
                if isinstance(raw_operations, list)
                else []
            )
            executable = [
                item for item in operations if not item.get("not_allowed_yet")
            ]
            record["analysis"] = {
                "goal_summary": str(plan.get("goal_summary") or ""),
                "test_plan": str(plan.get("test_plan") or ""),
                "requires_schema_change": bool(plan.get("requires_schema_change")),
            }
            record["final_operations"] = _summarize_operations(operations)
            if not executable:
                return self._finish(
                    record, "blocked", "planner_produced_no_executable_operations",
                )

            fingerprint = patch_fingerprint(operations)
            if fingerprint == previous_fingerprint:
                return self._finish(
                    record, "tests_failed", "repeated_patch_fingerprint",
                )
            previous_fingerprint = fingerprint
            record["final_patch_fingerprint"] = fingerprint

            # 改写候选代码之前：先把槽位标记为不可晋升，再落"即将应用"日志。
            # 顺序不能反——任何时刻崩溃都不能留下"可晋升但内容未验证"的槽位。
            self._mark_candidate_in_progress(record)
            # 应用前必须落日志（required）：写不进去就不改代码。
            self._write_journal(record, phase="applying", required=True)

            apply_result = self._apply(operations)
            changed_files = [
                str(item.get("target_file") or "")
                for item in executable
                if item.get("target_file")
            ]
            round_data = RepairRound(
                round_index=attempt,
                patch_fingerprint=fingerprint,
                changed_files=changed_files,
                applied=_count(apply_result.get("operations_applied")),
                failed=_count(apply_result.get("operations_failed")),
                test_ok=False,
                test_summary={},
            )

            if not apply_result.get("ok"):
                round_data.failure_reason = "apply_failed"
                record["rounds"].append(round_data.to_dict())
                record["candidate"]["candidate_root"] = str(
                    apply_result.get("candidate_root") or ""
                )
                return self._abort(
                    record, "apply_failed", _apply_failure_reason(apply_result),
                )

            # 活跃槽位可能在规划期间被切换：按执行器实际写入的槽位重新隔离，
            # 不能只依赖 apply 之前猜的槽位名。
            applied_slot = str(apply_result.get("inactive_slot") or "")
            if (
                applied_slot
                and baseline is not None
                and applied_slot != record["candidate"].get("candidate_slot")
            ):
                self._guard.mark_not_promotable(
                    applied_slot,
                    reason="repair_in_progress",
                    run_id=str(record.get("run_id") or ""),
                )
                record["candidate"]["candidate_slot"] = applied_slot

            candidate_root = str(apply_result.get("candidate_root") or "")
            record["candidate"]["candidate_root"] = candidate_root
            record["candidate"]["candidate_fingerprint"] = self._fingerprint_root(
                candidate_root
            )
            record["candidate"]["patch_fingerprint"] = fingerprint
            self._write_journal(record, phase="applied")
            test_report = self._run_tests(changed_files, candidate_root)
            round_data.test_ok = bool(test_report.get("ok"))
            round_data.test_summary = _summarize_tests(test_report)

            if test_report.get("ok"):
                record["rounds"].append(round_data.to_dict())
                return self._after_tests(
                    record, candidate_root, promote=promote, run_gate=run_gate,
                )

            round_data.failure_reason = "tests_failed"
            record["rounds"].append(round_data.to_dict())
            feedback = _extract_failure_feedback(test_report)
            self._write_journal(record, phase="tests_failed")

        # 轮次耗尽：候选槽位里是最后一轮失败的代码，必须回滚到基线。
        return self._abort(record, "tests_failed", "max_rounds_exhausted")

    # ── 各阶段 ────────────────────────────────────────────────────

    def _new_record(self, goal: str, trace_id: str | None) -> dict[str, Any]:
        """初始化一条可审计的运行记录。"""
        return {
            "run_id": uuid4().hex,
            "goal": goal,
            "trace_id": trace_id or str(uuid4()),
            "status": "started",
            "ok": False,
            "started_at": _now(),
            "finished_at": None,
            "max_rounds": self._max_rounds,
            "context": {},
            "analysis": {},
            "final_patch_fingerprint": None,
            "rounds": [],
            "gate": None,
            "promotion": None,
            "failure_reason": None,
            "candidate_root": None,
            "final_operations": [],
            "request_id": None,
            # 并发锁与候选版本绑定信息（审计/恢复的唯一依据）。
            "lock": {},
            "recovery": [],
            "phases": [],
            "candidate": {
                "baseline": None,
                "baseline_available": False,
                "candidate_slot": None,
                "candidate_root": None,
                "candidate_fingerprint": None,
                "patch_fingerprint": None,
                "verifications": [],
                "promotable": False,
                "recovery": None,
            },
        }

    def _collect_context(
        self, goal: str, files: list[str] | None, record: dict[str, Any]
    ) -> ProjectContext:
        """收集只读代码上下文；失败时降级为无上下文（不阻断规划）。"""
        try:
            context = self._reader.collect_context(goal, explicit_files=files)
        except Exception:
            logger.exception("代码上下文收集失败，降级为无上下文规划")
            context = ProjectContext()
        payload = context.to_dict().get("files")
        record["context"] = {
            "files": [
                str(item.get("path") or "")
                for item in payload
                if isinstance(item, dict)
            ] if isinstance(payload, list) else [],
            "keywords": list(context.keywords),
            "matches": len(context.matches),
            "truncated": context.truncated,
        }
        return context

    def _safe_plan(
        self,
        goal: str,
        trace_id: str | None,
        context: ProjectContext,
        previous_plan: dict[str, Any] | None,
        failure_feedback: str | None,
    ) -> dict[str, Any] | None:
        """调用规划器；异常或结构非法时返回 None，绝不伪造计划。"""
        try:
            plan = self._planner.plan(
                goal,
                trace_id=trace_id,
                context=context,
                previous_plan=previous_plan,
                failure_feedback=failure_feedback,
            )
            operations = plan.get("operations")
            if not isinstance(operations, list):
                return None
            return plan
        except Exception:
            logger.exception("自进化修复规划失败")
            return None

    def _apply(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """把补丁应用到非活跃槽位（唯一写入路径）。"""
        try:
            result = self._executor.apply_to_inactive(operations)
        except Exception as exc:
            logger.exception("补丁应用异常")
            return {
                "ok": False,
                "error": f"patch executor crashed: {exc}",
                "operations_applied": [],
                "operations_failed": [],
            }
        if not isinstance(result, dict):
            return {
                "ok": False,
                "error": "patch executor returned invalid result",
                "operations_applied": [],
                "operations_failed": [],
            }
        return result

    def _run_tests(
        self, changed_files: list[str], candidate_root: str
    ) -> dict[str, Any]:
        """在候选槽位上运行定向测试；异常按失败处理。"""
        try:
            runner = self._test_runner_factory(candidate_root)
            report = runner.run_for_changed_files(changed_files)
        except Exception as exc:
            logger.exception("定向测试执行异常")
            return {"ok": False, "error": f"targeted test runner crashed: {exc}"}
        if not isinstance(report, dict):
            return {"ok": False, "error": "targeted test runner returned invalid report"}
        return report

    def _after_tests(
        self,
        record: dict[str, Any],
        candidate_root: str,
        *,
        promote: bool,
        run_gate: bool,
    ) -> dict[str, Any]:
        """测试通过后：指纹复核 → 安全门 → 指纹复核 → （可选）受控晋升。"""
        verified = self._verify_candidate(record, stage="after_tests")
        if not verified["ok"]:
            return self._abort(record, "candidate_changed", str(verified["reason"]))

        if run_gate:
            try:
                gate_result = self._gate_runner(candidate_root)
            except Exception as exc:
                logger.exception("安全门执行异常")
                record["gate"] = {"ok": False, "error": f"gate raised: {exc}"}
                return self._abort(record, "gate_failed", "gate_raised")
            record["gate"] = _gate_summary(gate_result)
            if not _gate_ok(gate_result):
                return self._abort(record, "gate_failed", _gate_error(gate_result))
            # 闸门跑完再核一次：闸门/候选评测期间代码不得被改动。
            verified = self._verify_candidate(record, stage="after_gate")
            if not verified["ok"]:
                return self._abort(
                    record, "candidate_changed", str(verified["reason"])
                )

        self._mark_candidate_verified(record)

        if not promote:
            return self._finish(record, "tests_passed", None)

        # 进入晋升前最后一次核对：实际晋升的必须就是测试+闸门验证过的那份代码。
        verified = self._verify_candidate(record, stage="before_promote")
        if not verified["ok"]:
            return self._abort(record, "candidate_changed", str(verified["reason"]))

        self._write_journal(record, phase="promoting")
        try:
            promotion = self._promote_runner()
        except Exception as exc:
            logger.exception("晋升执行异常")
            record["promotion"] = {"ok": False, "error": f"promotion raised: {exc}"}
            # 晋升请求可能已经切换活跃槽位，此时不再回滚候选槽位（dirty=False）。
            return self._abort(
                record, "promote_failed", "promotion_raised", dirty=False,
            )

        record["promotion"] = (
            promotion if isinstance(promotion, dict)
            else {"ok": bool(promotion)}
        )
        status = record["promotion"].get("status")
        if status == "switch_requested":
            # 进程切换由 Supervisor 执行；这里只记录"已请求"，不声称已晋升。
            return self._finish(record, "promote_requested", None)
        if record["promotion"].get("ok"):
            return self._finish(record, "promoted", None)
        return self._finish(
            record,
            "promote_failed",
            str(record["promotion"].get("error") or "promotion_failed"),
        )

    # ── 候选版本隔离与中断恢复 ────────────────────────────────────

    @staticmethod
    def _fingerprint_root(root: str) -> str | None:
        """候选代码树指纹；路径为空或不存在时返回 None（即"无法验证"）。"""
        return tree_fingerprint(Path(root)) if root else None

    def _write_journal(
        self, record: dict[str, Any], *, phase: str, required: bool = False
    ) -> bool:
        """写运行日志；无基线时没有可恢复内容，直接跳过。

        required=True 用于改写代码之前的阶段：日志写不进去就不允许继续，
        否则中断后无法判断候选槽位该回滚到什么。
        """
        if not (record.get("lock") or {}).get("acquired"):
            return False
        if (record.get("candidate") or {}).get("baseline") is None:
            return False
        try:
            self._journal.write(record, phase=phase)
            return True
        except OSError:
            if required:
                raise
            logger.warning("修复运行日志写入失败: phase=%s", phase, exc_info=True)
            return False

    def _mark_candidate_in_progress(self, record: dict[str, Any]) -> None:
        """改写候选代码前把槽位标记为不可晋升（fail-closed）。"""
        baseline = Baseline.from_dict(
            (record.get("candidate") or {}).get("baseline")
        )
        if baseline is None:
            return
        slot = self._guard.candidate_slot()
        if slot is None:
            return
        record["candidate"]["candidate_slot"] = slot.name
        self._guard.mark_not_promotable(
            slot.name,
            reason="repair_in_progress",
            run_id=str(record.get("run_id") or ""),
        )

    def _mark_candidate_verified(self, record: dict[str, Any]) -> None:
        """候选通过测试/闸门后记录指纹，供晋升前核对。"""
        candidate = record.setdefault("candidate", {})
        fingerprint = candidate.get("candidate_fingerprint")
        slot = self._guard.candidate_slot()
        if not fingerprint or slot is None:
            return
        gate = record.get("gate")
        gate_ok = bool(gate.get("ok")) if isinstance(gate, dict) else bool(gate)
        self._guard.mark_promotable(
            slot.name,
            run_id=str(record.get("run_id") or ""),
            fingerprint=str(fingerprint),
            gate_ok=gate_ok,
        )
        candidate["promotable"] = True
        candidate["candidate_slot"] = slot.name

    def _verify_candidate(
        self, record: dict[str, Any], *, stage: str
    ) -> dict[str, Any]:
        """核对候选代码是否仍是"刚被测试/闸门验证过的那份"。

        同时核对基线：活跃槽位被切换或基线内容变化时，候选不再是可解释的
        产物，同样必须阻断晋升。无法指纹化（槽位未初始化）时如实记录
        fingerprint_unavailable，不冒充已验证。
        """
        candidate = record.setdefault("candidate", {})
        expected = candidate.get("candidate_fingerprint")
        root = candidate.get("candidate_root")
        actual = self._fingerprint_root(str(root)) if root else None
        detail: dict[str, Any] = {
            "stage": stage,
            "expected": expected,
            "actual": actual,
            "ok": False,
            "reason": None,
        }
        baseline = Baseline.from_dict(candidate.get("baseline"))
        if expected is None:
            if baseline is None:
                detail.update(ok=True, reason="fingerprint_unavailable")
            else:
                detail.update(reason="candidate_tree_missing")
        elif actual != expected:
            detail.update(reason="candidate_fingerprint_mismatch")
        else:
            drift = self._guard.baseline_drift(baseline) if baseline else None
            if drift is not None:
                detail.update(reason=drift)
            else:
                detail.update(ok=True)
        candidate.setdefault("verifications", []).append(detail)
        return detail

    def _restore_candidate(
        self, record: dict[str, Any], *, reason: str
    ) -> dict[str, Any]:
        """把候选槽位恢复为基线；恢复失败则显式隔离，绝不留下可误晋升的代码。"""
        candidate = record.setdefault("candidate", {})
        baseline = Baseline.from_dict(candidate.get("baseline"))
        recovery: dict[str, Any] = {"attempted": False, "reason": reason}
        if baseline is None:
            recovery["skipped"] = "no_baseline"
            candidate["recovery"] = recovery
            return recovery
        recovery["attempted"] = True
        result = self._guard.restore(baseline)
        recovery["result"] = result
        if not result.get("ok"):
            slot = self._guard.candidate_slot()
            if slot is not None:
                self._guard.mark_not_promotable(
                    slot.name,
                    reason="restore_failed: "
                    + str(result.get("reason") or "unknown"),
                    run_id=str(record.get("run_id") or ""),
                )
                recovery["quarantined"] = True
        else:
            candidate["promotable"] = False
        candidate["recovery"] = recovery
        return recovery

    def _abort(
        self,
        record: dict[str, Any],
        status: str,
        failure_reason: str | None,
        *,
        dirty: bool = True,
    ) -> dict[str, Any]:
        """失败收尾：先回滚候选槽位，再写终态（绝不声称成功）。"""
        if dirty:
            self._restore_candidate(record, reason=str(failure_reason or status))
        return self._finish(record, status, failure_reason)

    def _finish(
        self, record: dict[str, Any], status: str, failure_reason: str | None
    ) -> dict[str, Any]:
        """写入终态、落运行日志并返回记录。"""
        record["status"] = status
        record["ok"] = status in _SUCCESS_STATUSES
        record["failure_reason"] = failure_reason
        record["finished_at"] = _now()
        self._write_journal(record, phase="finished")
        return record


def persist_repair_run(db: Session, record: dict[str, Any]) -> SelfDevRequest:
    """把修复运行记录落库（复用 SelfDevRequest / PatchOperation）。

    ``plan["repair"]`` 保存完整执行链；``PatchOperation`` 只保存最终补丁的操作摘要，
    不保存文件内容（内容已在候选槽位中，避免数据库膨胀）。
    """
    request = SelfDevRequest(
        trace_id=str(record.get("trace_id") or uuid4()),
        goal=str(record.get("goal") or ""),
        status=str(record.get("status") or "unknown"),
        plan={"repair": record},
    )
    db.add(request)
    db.flush()

    operations = record.get("final_operations")
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            db.add(PatchOperation(
                request_id=request.id,
                operation=str(operation.get("operation") or "add_file"),
                target_file=str(operation.get("target_file") or ""),
                reason=str(operation.get("reason") or ""),
                risk_notes=str(operation.get("risk_notes") or ""),
                requires_schema_change=bool(
                    operation.get("requires_schema_change")
                ),
                safe_delete_scope=operation.get("safe_delete_scope"),
                not_allowed_yet=bool(operation.get("not_allowed_yet")),
            ))

    record["request_id"] = request.id
    db.commit()
    return request
