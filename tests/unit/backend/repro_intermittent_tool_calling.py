"""间歇性 Chat-to-Tool 调用的复现脚本。

目的：使用真实的 ActionPlanner + ToolRegistry 类，演示相同的用户输入有时会调用工具、
有时却不会——无需真实的 LLM 或数据库。

如何模拟真实场景
-----------------
当前运行时有 2 个 LLM 决策点：

1) ActionPlanner.plan() → LLM，temperature=0.1，任何解析错误时返回
   AgentDecision.default_response()，即 {decision_type:"final_response",
   execution_mode:"explain_only", should_execute:False}。因此 should_execute/
   candidate_tool 受非确定性调用影响。

2) AgentGraph.run() 内的主 Agent LLM 调用未传 temperature 参数，使用模型默认值（高方差）。
   模型被要求输出 <tool_call>...</tool_call> 标签或自然语言。
   本脚本手动解析 <tool_call> 并通过 ToolRegistry.execute() 执行。

关键：AgentDecision 仅作为**拦截器**使用。没有任何代码路径会在
should_execute=True 时强制调用工具。如果主 LLM 以自然语言回复（无 <tool_call> 标签），
循环会将该回复作为 FINAL 返回，工具永远不会执行。

此脚本注入**规划器不稳定性**（有时规划器输出不可解析 → 安全回退 should_execute=False）
和**主 LLM 不稳定性**（有时主 LLM 输出工具标签，有时输出自然语言）。
这两个开关忠实地模拟了真实模型非零温度的行为。
然后对相同输入运行 10 次迭代，记录工具是否实际执行。

运行方式：
    cd backend && python tests/unit/backend/repro_intermittent_tool_calling.py
"""

import json
import random
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

# 使 backend/ 目录下的 aiive 可导入
sys.path.insert(0, ".")

from aiive.core.action_planner import ActionPlanner, AgentDecision  # noqa: E402
from aiive.core.llm_client import LLMResponse  # noqa: E402
from aiive.tools.registry import get_tool_registry, ToolRegistration, CapabilitySafetySchema, compute_descriptor_hash  # noqa: E402


@dataclass
class _Call:
    name: str = ""
    params: dict = field(default_factory=dict)


# 全局 spy：记录通过注册表进行的真实工具调用。
SPY: list[_Call] = []


def _spy_handler(**params):
    SPY.append(_Call(name=_spy_handler.__name__.split("_")[-1], params=params))
    return {"ok": True, "result": {"spy": True, "params": params}}


def _install_spy_tools():
    """用 spy 处理器覆盖几个内置工具（仅脚本内有效，修改全局状态）。"""
    reg = get_tool_registry()
    for cap_id in ("schedule_reminder", "list_tasks", "cancel_task", "remember_or_update"):
        safety = CapabilitySafetySchema(
            capability_id=cap_id,
            definition_source="local_builtin",
            definition_trust_level="trusted",
            risk_level="low",
            requires_confirmation=False,
            writes_external_world=False,
            can_delete=False,
            descriptor_hash=compute_descriptor_hash({"capability_id": cap_id}),
        )
        reg.register(ToolRegistration(
            safety=safety,
            handler=_spy_handler,
            description="spy",
            parameters={},
        ))


class FlakyLLM:
    """模拟 temperature>0 非确定性的 Duck-typed LLM。

    - 规划器提示（"Action Planner"）：以 planner_ok 概率返回有效决策 JSON，
      否则返回不可解析文本 → 触发 ActionPlanner 安全回退（should_execute=False）。
    - 主提示：以 main_tool 概率返回 <tool_call> 标签，
      否则返回自然语言"假成功"回复。
    """

    def __init__(self, planner_ok: float, main_tool: float, seed: int = 0):
        self._planner_ok = planner_ok
        self._main_tool = main_tool
        self._rng = random.Random(seed)

    def chat(self, messages, model=None, temperature=None, timeout=None, trace_id=None) -> LLMResponse:
        text = "\n".join(m.get("content", "") for m in messages)
        is_planner = "Action Planner" in text

        if is_planner:
            if self._rng.random() < self._planner_ok:
                payload = {
                    "decision_type": "tool_call",
                    "execution_mode": "execute",
                    "intent_type": "reminder_create",
                    "should_execute": True,
                    "tool_name": "schedule_reminder",
                    "tool_params": {"content": "赫赫", "delay_minutes": 1},
                    "confidence": 0.95,
                    "reason": "reminder request",
                }
                content = json.dumps(payload)
            else:
                content = "I am not sure how to answer that."  # → 解析错误 → 回退
        else:
            if self._rng.random() < self._main_tool:
                content = (
                    '<tool_call>{"name":"schedule_reminder",'
                    '"params":{"content":"赫赫","delay_minutes":1}}</tool_call>'
                )
            else:
                content = "已经安排上了。"

        return LLMResponse(
            content=content,
            model="flaky-sim",
            latency_ms=1.0,
            usage={},
            raw_preview=content[:200],
            trace_id=trace_id or str(uuid.uuid4()),
        )


def simulate_one_iteration(msg: str, planner_ok: float, main_tool: float, seed: int):
    """使用 ActionPlanner + ToolRegistry 模拟工具调用执行。"""
    SPY.clear()
    llm = FlakyLLM(planner_ok=planner_ok, main_tool=main_tool, seed=seed)
    planner = ActionPlanner(llm)
    decision: AgentDecision = planner.plan(user_message=msg)

    # 主 LLM 回复（同一 FlakyLLM 实例再抽一次）
    main_reply = llm.chat([{"role": "user", "content": msg}], trace_id=str(uuid.uuid4())).content

    # 解析 <tool_call> XML 标签，通过 ToolRegistry 执行
    import re
    registry = get_tool_registry()
    records: list[dict] = []
    malformed = False
    cleaned = main_reply

    tool_pattern = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
    matches = tool_pattern.findall(main_reply)
    for match in matches:
        try:
            parsed = json.loads(match.strip())
            tool_name = parsed.get("name", "")
            params = parsed.get("params", {})
            result = registry.execute(
                tool_name, params,
                instruction_source="trusted_user_command",
            )
            if result.get("ok"):
                records.append({"name": tool_name, "status": "completed"})
            elif result.get("approval_required"):
                records.append({"name": tool_name, "status": "blocked"})
            else:
                records.append({"name": tool_name, "status": "failed"})
            cleaned = tool_pattern.sub("", cleaned, count=1)
        except (json.JSONDecodeError, KeyError):
            malformed = True
            records.append({"name": "unknown", "status": "failed"})

    cleaned = cleaned.strip() or main_reply
    executed = any(r["status"] in ("completed", "failed") for r in records)
    blocked = any(r["status"] == "blocked" for r in records)

    return {
        "input": msg,
        "planner_should_execute": decision.should_execute,
        "planner_decision_type": decision.decision_type,
        "planner_candidate_tool": decision.tool_name,
        "main_reply": main_reply,
        "tool_executed": executed,
        "tool_blocked": blocked,
        "spy_calls": [c.name for c in SPY],
        "records": records,
        "malformed": malformed,
        "final_reply": cleaned,
        "tasks_table_changed": bool(SPY),
    }


def run_matrix(msg: str, n: int = 10, planner_ok: float = 0.8, main_tool: float = 0.7):
    print("=" * 90)
    print(f"复现矩阵 — 输入: {msg!r}  (n={n})")
    print(f"(planner_ok={planner_ok} 模拟 ActionPlanner temperature=0.1 解析成功率)")
    print(f"(main_tool={main_tool} 模拟主 LLM 默认温度下工具标签输出率)")
    print("=" * 90)
    header = f"{'#':>2} | {'planner.should_execute':>24} | {'tool_executed':>13} | {'blocked':>7} | final_reply"
    print(header)
    print("-" * 90)
    rows = []
    for i in range(n):
        # 每次迭代改变 seed 以模拟生产环境中的非确定性行为
        row = simulate_one_iteration(msg, planner_ok, main_tool, seed=1000 + i)
        rows.append(row)
        print(
            f"{i+1:>2} | "
            f"{str(row['planner_should_execute']):>24} | "
            f"{str(row['tool_executed']):>13} | "
            f"{str(row['tool_blocked']):>7} | "
            f"{row['final_reply'][:40]}"
        )
    # 汇总
    exec_yes = sum(1 for r in rows if r["tool_executed"])
    exec_no = n - exec_yes
    print("-" * 90)
    print(f"汇总: 工具在 {exec_yes}/{n} 次运行中执行; {exec_no}/{n} 次未执行。")
    if exec_yes and exec_no:
        print(">>> 间歇性 BUG 复现: 相同输入有时调用工具，有时不调用。")
    else:
        print(">>> （在这些不稳定性参数下运行一致；增大方差可复现。）")
    print()
    return rows


if __name__ == "__main__":
    _install_spy_tools()
    msgs = [
        "1 分钟后提醒我 赫赫",
        "一分钟后提醒我 赫赫",
        "我有哪些提醒？",
    ]
    all_rows = {}
    for m in msgs:
        all_rows[m] = run_matrix(m, n=10)
    # 输出 JSON 以备审计报告使用
    with open("tests/artifacts/repro_matrix.json", "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)
    print("已写入 tests/artifacts/repro_matrix.json")
