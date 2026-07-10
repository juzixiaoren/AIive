"""V20 StewardSignalExtractor：个人管家信号提取器。

记忆系统是 AIive 的核心组件，负责 Agent 的长期记忆管理和检索。
本模块专注于从对话中提取用户的个人管家信号——包括日常习惯、偏好、
身份信息和日程安排等。与 MemoryExtractor 的区别在于，本模块更侧重于
具有时间规律（routine/schedule）和长期模式（habit）的信号。

提取信号类型：
- user_profile: 用户身份信息（年龄、职业、地点等）
- preference: 用户偏好（喜欢/不喜欢/习惯）
- routine: 重复行为（每天/每周/工作日）
- habit: 习惯模式
- schedule: 日程安排（带定时信息）
"""

import json
import logging
from typing import Any

from json_repair import repair_json

from aiive.core.llm_client import LLMClient, LLMResponse

logger = logging.getLogger(__name__)

# 管家信号提取的 LLM 提示词模板
# 专注于提取用户在对话中明示的日常习惯、偏好和个人信息
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
    """个人管家信号提取器：从对话中提取用户的日常习惯和偏好信号。

    使用 LLM 分析对话内容，识别用户明示的周期性行为、偏好模式和个人信息。
    与 MemoryExtractor 互补：MemoryExtractor 提取通用结构化记忆，
    本提取器专注于管家场景下的个人信号。
    """

    def __init__(self, llm_client: LLMClient):
        """初始化管家信号提取器。

        Args:
            llm_client: LLM 客户端实例。
        """
        self._llm_client: LLMClient = llm_client

    def extract(
        self, user_message: str, reply: str,         trace_id: str | None = None
    ) -> list[dict[str, Any]]:
        """从用户消息和助手回复中提取管家信号。

        Args:
            user_message: 用户发送的原始消息。
            reply: 助手生成的回复内容。
            trace_id: 可选的追踪 ID。

        Returns:
            信号字典列表，每个字典包含 signal_type、content、confidence 等字段。
            无有效信号时返回空列表。
        """
        prompt = STEWARD_PROMPT.format(user_message=user_message, reply=reply)
        messages = [{"role": "user", "content": prompt}]

        try:
            response: LLMResponse = self._llm_client.chat(
                messages, trace_id=trace_id, temperature=0.1
            )
        except Exception:
            logger.exception("管家信号提取LLM调用失败: trace_id=%s", trace_id)
            raise
        return self._parse(response.content)

    def _parse(self, raw: str) -> list[dict[str, Any]]:
        """解析 LLM 原始输出为信号字典列表。

        处理 markdown 代码块包装，并过滤出合法的信号类型。
        解析失败时静默返回空列表。

        Args:
            raw: LLM 返回的原始文本。

        Returns:
            合法的信号字典列表。
        """
        try:
            text = raw.strip()
            if not text:
                return []
            # 去除可能的 markdown 代码块标记
            if text.startswith("```"):
                lines = text.split("\n")
                # 去掉首行（``` 或 ```json 等）
                # 去掉尾行（```）
                inner_lines = lines[1:]
                if inner_lines and inner_lines[-1].strip() == "```":
                    inner_lines = inner_lines[:-1]
                text = "\n".join(inner_lines).strip()
                if not text:
                    return []
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                repaired = repair_json(text)
                result = json.loads(repaired)
            if isinstance(result, list):
                # 过滤出合法的信号类型
                return [
                    s for s in result
                    if s.get("signal_type") in {
                        "user_profile", "preference", "routine", "habit", "schedule"
                    }
                ]
            return []
        except (json.JSONDecodeError, ValueError):
            logger.warning("管家信号提取JSON解析失败", exc_info=True)
            return []
