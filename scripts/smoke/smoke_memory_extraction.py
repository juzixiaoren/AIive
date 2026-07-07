#!/usr/bin/env python3
"""Smoke script: test memory extraction pipeline."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import httpx


def main():
    base = "http://127.0.0.1:8000"

    # Test 1: explicit memory command → should create active memory
    r1 = httpx.post(
        f"{base}/api/chat",
        json={"message": "记住，我叫小明，我喜欢喝咖啡"},
        timeout=30,
    )
    data1 = r1.json()
    thread_id = data1["thread_id"]
    print("Test 1 - Explicit memory:", json.dumps({
        "reply": data1["reply"][:100],
        "thread_id": thread_id,
    }, ensure_ascii=False))

    # Test 2: check memories API
    r2 = httpx.get(f"{base}/api/memories", timeout=5)
    memories = r2.json()
    active = [m for m in memories if m["lifecycle_state"] == "active"]
    print(f"\nMemories total: {len(memories)}, active: {len(active)}")
    for m in memories:
        print(f"  [{m['lifecycle_state']}] {m['content']} (confidence: {m['confidence']})")

    # Test 3: casual chat → should NOT create active memory
    r3 = httpx.post(
        f"{base}/api/chat",
        json={"message": "你好，今天天气怎么样？", "thread_id": thread_id},
        timeout=30,
    )
    data3 = r3.json()
    print(f"\nTest 3 - Casual chat: {data3['reply'][:100]}")

    # Check trace for memory events
    trace_id = data3["trace_id"]
    r4 = httpx.get(f"{base}/api/debug/traces/{trace_id}", timeout=5)
    trace = r4.json()
    memory_events = [e for e in trace.get("events", []) if "memory" in e["event_type"]]
    print(f"Memory events in trace: {len(memory_events)}")
    for e in memory_events:
        print(f"  {e['event_type']}: {json.dumps(e['payload'], ensure_ascii=False)[:120]}")

    print("\n" + ("SMOKE PASSED" if len(active) > 0 else "SMOKE FAILED"))
    return 0 if len(active) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
