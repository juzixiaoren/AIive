"""手动集成冒烟脚本：真实 npm 安装 + 真实 MCP stdio 链路（不进测试套件）。

前置条件：
- 已安装 node/npm（PATH 可见）
- 数据库可用（走项目配置的 DATABASE_URL；也可用环境变量指到 SQLite）

用法（从 backend 目录）：
    D:/miniconda/envs/aiive/python.exe scripts/smoke/smoke_mcp_real.py

流程：
1. discovery 搜索 @modelcontextprotocol/server-memory（无需 API key）
2. installer.install_sandbox 真实 npm install 到 .data/mcp_sandbox/
3. runtime_client 启动 stdio 子进程 → initialize → tools/list
4. tools/call read_graph（无副作用）
5. installer.run_smoke 推进状态机 → bootstrap 注册进 ToolRegistry
6. 经 registry.execute 走一遍 Agent 同款调用路径
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# 默认用独立 SQLite，避免污染开发库；要用真实库可先 set SMOKE_USE_PROJECT_DB=1
if not os.environ.get("SMOKE_USE_PROJECT_DB"):
    os.environ.setdefault("DATABASE_URL", "sqlite:///./.smoke_mcp_real.db")

TARGET = "@modelcontextprotocol/server-memory"


def main() -> int:
    from aiive.db.base import SessionLocal, engine
    from aiive.db.models import Base
    from aiive.mcp.bootstrap import activate_capability_tools
    from aiive.mcp.discovery import get_candidate_by_name
    from aiive.mcp.installer import install_sandbox, run_smoke
    from aiive.mcp.runtime_client import build_launch_spec, get_runtime_client
    from aiive.tools.registry import ToolRegistry

    Base.metadata.create_all(engine)
    db = SessionLocal()

    print(f"[1/6] discovery: {TARGET}")
    candidate = get_candidate_by_name(TARGET)
    assert candidate is not None, "catalog missing target"
    print(f"      trust={candidate.definition_trust_level} declared={candidate.declared_tools}")

    print("[2/6] real npm install ...")
    result = install_sandbox(
        db, candidate.name, candidate.package_ref, candidate.version, candidate.transport,
        candidate.declared_tools,
        {"name": candidate.name, "description": candidate.description,
         "trust_level": candidate.definition_trust_level},
        env_keys=candidate.required_env,
    )
    db.commit()
    print(f"      {json.dumps(result, ensure_ascii=False)[:400]}")
    if not result.get("ok"):
        print("INSTALL FAILED")
        return 1

    from aiive.db.models import Capability
    cap = db.query(Capability).filter(
        Capability.capability_id == f"mcp:{candidate.name}"
    ).one()

    print("[3/6] spawn stdio server + tools/list ...")
    client = get_runtime_client()
    client.configure(cap.capability_id, build_launch_spec(cap.definition["launch"]))
    tools = client.list_tools(cap.capability_id, timeout=60)
    names = [t["name"] for t in tools]
    print(f"      real tools ({len(names)}): {names}")

    print("[4/6] tools/call read_graph ...")
    call = client.call_tool(cap.capability_id, "read_graph", {}, timeout=60)
    print(f"      ok={call.ok} untrusted={call.untrusted} result={str(call.result)[:200]} error={call.error}")

    print("[5/6] run_smoke -> state machine ...")
    definition = dict(cap.definition)
    definition["tools"] = tools
    definition["risk_verdict"] = "medium"
    cap.definition = definition
    smoke = run_smoke(db, cap.capability_id, {
        "ok": bool(names) and call.ok,
        "mode": "tools_call",
        "real_tools": names,
        "error": call.error,
    })
    db.commit()
    print(f"      state={smoke.get('state')} smoke_passed={smoke.get('smoke_passed')}")

    print("[6/6] register into ToolRegistry + execute ...")
    registry = ToolRegistry()
    activation = activate_capability_tools(registry, cap)
    print(f"      registered: {activation.get('registered')}")
    outcome = registry.execute(
        "mcp_server_memory_read_graph", {}, "trusted_user_command",
    )
    print(f"      registry.execute ok={outcome.get('ok')} result={str(outcome.get('result'))[:200]}")

    client.close()
    db.close()
    passed = (
        result.get("ok") and bool(names) and call.ok
        and smoke.get("smoke_passed") and activation.get("ok")
        and outcome.get("ok")
    )
    print("SMOKE PASS" if passed else "SMOKE FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
