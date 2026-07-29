"""CapabilityPlanner: 从用户目标分析缺失能力，搜索、评估、计划安装 MCP。

接入新记忆系统：安装成功/失败后写入 procedural/episodic 记忆。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from json_repair import repair_json

from aiive.core.llm_client import LLMClient, LLMResponse
from aiive.core.text_utils import strip_code_fence
from aiive.prompts import get_prompt_registry

logger = logging.getLogger(__name__)

@dataclass
class RiskScore:
    """MCP 候选的风险评分。"""
    source_trust: float = 0.5
    version_stability: float = 0.5
    permission_risk: float = 0.5
    tool_count_risk: float = 0.5
    overall: float = 0.5
    verdict: str = "medium"


@dataclass
class CandidateEvaluation:
    """单个 MCP 候选的评估结果。"""
    candidate_name: str
    source: str
    version: str
    risk_score: RiskScore = field(default_factory=RiskScore)
    recommendation: str = ""  # recommended / needs_review / rejected


@dataclass
class InstallPlan:
    """能力安装计划。"""
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = ""
    goal_summary: str = ""
    missing_capability_type: str = ""
    candidates: list[CandidateEvaluation] = field(default_factory=list)
    selected: str = ""
    risk_summary: str = ""
    status: str = "evaluated"  # proposed → evaluated → installed → activated → failed


class CapabilityPlanner:
    """能力规划器：分析目标 → 搜索候选 → 评估风险 → 生成安装计划。"""

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm: LLMClient = llm_client

    def analyze_goal(self, goal: str) -> dict[str, Any]:
        """调用 LLM 分析用户目标，识别缺失的能力类型。"""
        prompt = get_prompt_registry().render(
            "mcp.capability_plan",
            goal=goal,
        ).content
        try:
            resp: LLMResponse = self._llm.chat(
                [{"role": "user", "content": prompt}], temperature=0.1,
            )
            return self._parse_analysis(resp.content)
        except Exception:
            logger.warning("能力目标分析失败，使用默认规划: goal=%s", goal[:80], exc_info=True)
            return {
                "goal_summary": goal[:80],
                "missing_capability_type": "other",
                "search_keywords": goal.split()[:3],
                "risk_tolerance": "medium",
                "reasoning": "Analysis failed, using defaults",
            }

    def search_candidates(self, keywords: list[str]) -> list[dict[str, Any]]:
        """搜索 MCP 候选。"""
        from aiive.mcp.discovery import search_mcp_candidates
        query = " ".join(keywords)
        candidates = search_mcp_candidates(query)
        return [
            {
                "name": c.name, "source": c.source, "version": c.version,
                "description": c.description, "transport": c.transport,
                "declared_tools": c.declared_tools,
                "risk_notes": c.risk_notes,
                "definition_trust_level": c.definition_trust_level,
                "descriptor_hash": c.descriptor_hash,
            }
            for c in candidates
        ]

    def evaluate_candidates(
        self, raw_candidates: list[dict[str, Any]]
    ) -> list[CandidateEvaluation]:
        """对每个候选做风险评估。"""
        results: list[CandidateEvaluation] = []
        for c in raw_candidates:
            risk = self._compute_risk(c)
            rec = self._recommendation(risk)
            results.append(CandidateEvaluation(
                candidate_name=c.get("name", ""),
                source=c.get("source", "unknown"),
                version=c.get("version", ""),
                risk_score=risk,
                recommendation=rec,
            ))
        # 按 overall 升序排列（低风险在前）
        results.sort(key=lambda e: e.risk_score.overall)
        return results

    def plan_from_goal(self, goal: str) -> InstallPlan:
        """端到端规划：先分析目标提取搜索关键词，再用关键词搜索、评估、生成计划。

        修复历史缺陷：analyze_goal 产出的 search_keywords 曾从未被用于搜索
        （generate_plan 在搜索完成后才调用它）。此方法把分析提前，用
        LLM 提取的关键词驱动候选搜索，仅调用一次 LLM。
        """
        analysis = self.analyze_goal(goal)
        keywords = [str(k) for k in analysis.get("search_keywords", []) if str(k).strip()]
        if not keywords:
            keywords = goal.split()[:5]
        candidates_raw = self.search_candidates(keywords)
        # 关键词太窄搜不到时，退回用原始目标全文搜索
        if not candidates_raw:
            candidates_raw = self.search_candidates([goal])
        evaluations = self.evaluate_candidates(candidates_raw)
        return self.generate_plan(goal, candidates_raw, evaluations, analysis=analysis)

    def generate_plan(
        self, goal: str, _candidates_raw: list[dict[str, Any]],
        evaluations: list[CandidateEvaluation],
        analysis: dict[str, Any] | None = None,
    ) -> InstallPlan:
        """生成完整安装计划。

        参数:
            analysis: 可选的预计算目标分析结果（plan_from_goal 传入，
                      避免重复调用 LLM）；为 None 时内部调用 analyze_goal。
        """
        if analysis is None:
            analysis = self.analyze_goal(goal)
        plan = InstallPlan(
            goal=goal,
            goal_summary=analysis.get("goal_summary", ""),
            missing_capability_type=analysis.get("missing_capability_type", ""),
            candidates=evaluations,
            status="evaluated",
        )
        # 选第一个低风险推荐候选
        recommended = [e for e in evaluations if e.recommendation == "recommended"]
        if recommended:
            plan.selected = recommended[0].candidate_name
            plan.risk_summary = f"推荐 {plan.selected} (risk={recommended[0].risk_score.overall:.1f})"
        elif evaluations:
            plan.selected = evaluations[0].candidate_name
            plan.risk_summary = "无低风险候选，需人工审查"
        return plan

    # ------------------------------------------------------------------
    # Risk scoring
    # ------------------------------------------------------------------

    def _compute_risk(self, candidate: dict[str, Any]) -> RiskScore:
        """计算候选风险评分。

        校准说明：信任映射 semi_trusted=0.7（官方注册表来源），配合下方
        verdict 阈值（low ≤ 0.35），使官方来源、低权限风险的候选可以达到
        "low"→"recommended"。历史缺陷：semi_trusted=0.5 时公式最优
        overall≈0.33 > 0.25，"recommended" 在数学上不可达。
        """
        trust = candidate.get("definition_trust_level", "semi_trusted")
        source_trust = {"trusted": 0.9, "semi_trusted": 0.7, "untrusted": 0.2}.get(trust, 0.3)
        declared_tools = candidate.get("declared_tools", [])
        tool_count = len(declared_tools) if isinstance(declared_tools, list) else 0
        tool_risk = min(1.0, tool_count / 20.0)
        perm_risk = 0.3
        risk_notes = str(candidate.get("risk_notes", "")).lower()
        if any(w in risk_notes for w in ("delete", "write", "network", "exec", "shell")):
            perm_risk = 0.7
        stability = 0.7
        overall = 1.0 - (source_trust * 0.4 + stability * 0.2 + (1.0 - perm_risk) * 0.2 + (1.0 - tool_risk) * 0.2)
        verdict = "low"
        if overall > 0.65:
            verdict = "critical"
        elif overall > 0.5:
            verdict = "high"
        elif overall > 0.35:
            verdict = "medium"
        return RiskScore(
            source_trust=source_trust, version_stability=stability,
            permission_risk=perm_risk, tool_count_risk=tool_risk,
            overall=round(overall, 2), verdict=verdict,
        )

    @staticmethod
    def _recommendation(risk: RiskScore) -> str:
        if risk.verdict in ("critical", "high"):
            return "needs_review"
        if risk.verdict == "medium":
            return "needs_review"
        return "recommended"

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_analysis(raw: str) -> dict[str, Any]:
        text = strip_code_fence(raw)
        try:
            try:
                data: dict[str, Any] = json.loads(text)
            except json.JSONDecodeError:
                data = json.loads(repair_json(text))
            return {
                "goal_summary": str(data.get("goal_summary", "")),
                "missing_capability_type": str(data.get("missing_capability_type", "other")),
                "search_keywords": list(data.get("search_keywords", [])),
                "risk_tolerance": str(data.get("risk_tolerance", "medium")),
                "reasoning": str(data.get("reasoning", "")),
            }
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"goal_summary": "", "missing_capability_type": "other", "search_keywords": [], "risk_tolerance": "medium", "reasoning": ""}
