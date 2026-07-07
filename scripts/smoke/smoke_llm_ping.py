#!/usr/bin/env python3
"""Smoke script: test real LLM connectivity and output structured JSON result."""

import json
import os
import sys

# Ensure backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from aiive.core.llm_client import LLMClient, LLMClientError
from aiive.config import settings


def main():
    client = LLMClient(
        base_url=settings.aiive_llm_base_url,
        api_key=settings.aiive_llm_api_key,
        default_model=settings.aiive_llm_model,
        timeout_seconds=settings.aiive_llm_timeout_seconds,
    )

    messages = [
        {"role": "user", "content": "Say exactly: AIive smoke test OK."},
    ]

    try:
        response = client.chat(messages)
        result = {
            "ok": True,
            "model": response.model,
            "latency_ms": response.latency_ms,
            "trace_id": response.trace_id,
            "reply_preview": response.content[:200],
        }
    except LLMClientError as e:
        result = {
            "ok": False,
            "error": str(e),
            "trace_id": e.trace_id,
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
