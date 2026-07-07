import json
from typing import Sequence

from aiive.core.llm_client import LLMClient, LLMResponse

STEWARD_PROMPT = (
    "Extract personal steward signals from the conversation. Focus on routines, "
    "preferences, and profile information the user explicitly stated.\n\n"
    "Output a JSON array of objects with keys:\n"
    "- signal_type: one of user_profile, preference, routine, habit, schedule\n"
    "- content: the signal as a sentence\n"
    "- confidence: 0.0-1.0\n"
    "- schedule_text (optional): for routines/schedules, describe timing (e.g. \"daily at 8am\")\n\n"
    "Routine signals = recurring behaviors (每天/每周/工作日/every/day/every week)\n"
    "Preference signals = likes/dislikes/habits (喜欢/不喜欢/习惯/prefer)\n"
    "Profile signals = identity info (年龄/职业/地点/age/job/location).\n\n"
    "Do NOT extract facts that are NOT routines/preferences/profile.\n"
    "If nothing qualifies, return [].\n\n"
    "Conversation:\n"
    "User: {user_message}\n"
    "Assistant: {reply}\n\n"
    "Output ONLY valid JSON:"
)


class StewardSignalExtractor:
    def __init__(self, llm_client: LLMClient):
        self._llm_client = llm_client

    def extract(
        self, user_message: str, reply: str, trace_id: str | None = None
    ) -> list[dict]:
        prompt = STEWARD_PROMPT.format(user_message=user_message, reply=reply)
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
                return [
                    s for s in result
                    if s.get("signal_type") in {
                        "user_profile", "preference", "routine", "habit", "schedule"
                    }
                ]
            return []
        except (json.JSONDecodeError, ValueError):
            return []
