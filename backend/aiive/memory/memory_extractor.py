import json
from typing import Sequence

from aiive.core.llm_client import LLMClient, LLMResponse

EXTRACT_PROMPT = (
    "Extract personal facts and preferences about the user from the conversation below. "
    "Output a JSON array of objects with keys: content (the fact/preference as a sentence), "
    "memory_type (one of: name, preference, habit, fact, schedule), "
    "confidence (0.0-1.0).\n\n"
    "If nothing is extractable, return an empty array []. "
    "Only extract explicit information the user stated, do NOT infer or guess.\n\n"
    "Conversation:\n"
    "User: {user_message}\n"
    "Assistant: {reply}\n\n"
    "Output ONLY valid JSON, no markdown, no explanation:"
)


class MemoryExtractor:
    def __init__(self, llm_client: LLMClient):
        self._llm_client = llm_client

    def extract(
        self, user_message: str, reply: str, trace_id: str | None = None
    ) -> list[dict]:
        prompt = EXTRACT_PROMPT.format(user_message=user_message, reply=reply)
        messages = [{"role": "user", "content": prompt}]

        response: LLMResponse = self._llm_client.chat(
            messages, trace_id=trace_id, temperature=0.1
        )
        return self._parse(response.content)

    def _parse(self, raw: str) -> list[dict]:
        try:
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1])
            result = json.loads(text)
            if isinstance(result, list):
                return result
            return []
        except (json.JSONDecodeError, ValueError):
            return []
