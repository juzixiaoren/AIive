#!/usr/bin/env python3
"""Smoke script: test Chat API with real LLM."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

import httpx


def main():
    base = "http://127.0.0.1:8000"

    # Check health
    try:
        r = httpx.get(f"{base}/health", timeout=5)
        health = r.json()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Health check failed: {e}"}, indent=2))
        return 1

    if not health.get("ok"):
        print(json.dumps({"ok": False, "error": "Health check not ok"}, indent=2))
        return 1

    # Call chat
    try:
        r = httpx.post(
            f"{base}/api/chat",
            json={"message": "Say exactly: Smoke test passed."},
            timeout=30,
        )
        data = r.json()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Chat API failed: {e}"}, indent=2))
        return 1

    result = {
        "ok": r.status_code == 200,
        "status_code": r.status_code,
        "reply_preview": data.get("reply", "")[:200],
        "thread_id": data.get("thread_id"),
        "trace_id": data.get("trace_id"),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
