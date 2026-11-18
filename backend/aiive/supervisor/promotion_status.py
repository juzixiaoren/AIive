"""Interpret a single promotion receipt and its observation window."""
import re

from aiive.supervisor.client import SupervisorClient


def promotion_status(request_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return "unavailable"
    client = SupervisorClient()
    receipt = client.result(request_id)
    if not receipt:
        return "requested"
    if receipt.get("kind") != "promote":
        return "unavailable"
    if receipt.get("status") in {"promoted", "rolled_back", "failed"}:
        return str(receipt["status"])
    if not receipt.get("ok"):
        return "rolled_back" if (receipt.get("rollback") or {}).get("ok") else "failed"
    if receipt.get("status") != "switched":
        return "requested"
    state = client.state()
    if not state.get("active_slot"):
        return "unavailable"
    if state.get("active_slot") != receipt.get("active_slot"):
        return "rolled_back"
    process = state.get("process") or {}
    if process.get("pid") != receipt.get("pid"):
        # The slot is no longer the process that this receipt verified.
        return "unavailable"
    return "observing" if state.get("monitor_until") else "promoted"
