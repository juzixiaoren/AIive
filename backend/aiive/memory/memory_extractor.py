"""V20 MemoryExtractor：基于 LLM 的结构化记忆提取器。

记忆系统是 AIive 的核心组件，负责 Agent 的长期记忆管理和检索。
本模块负责从对话中提取结构化记忆，输出 ExtractedMemory 并进行语义键解析。

记忆键规范（单一事实来源）见 aiive.memory.memory_types.MEMORY_KEY_GUIDE，
EXTRACT_PROMPT 直接引用它，避免键映射在多处重复维护。
"""

import json
import logging
from typing import Any

from json_repair import repair_json
from pydantic import BaseModel, Field

from aiive.core.llm_client import LLMClient, LLMResponse
from aiive.memory.memory_types import MEMORY_KEY_GUIDE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ExtractedMemory - 提取出的结构化记忆数据模型
# ---------------------------------------------------------------------------

class ExtractedMemory(BaseModel):
    """从对话中提取的单条结构化记忆。"""
    content: str  # 记忆内容文本
    memory_type: str  # 记忆类型：user_profile | agent_self | preference | project_decision | policy | environment | procedural | episodic
    memory_key: str = ""  # 去重用的稳定键（点号分隔，如 user.display_name）
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)  # 置信度，0.0-1.0
    source_span: str = ""  # 原文片段，用于追溯
    durable: bool = True  # 是否为持久记忆


# ---------------------------------------------------------------------------
# EXTRACT_PROMPT - LLM 提取提示词模板
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """从对话中提取关于用户的持久性个人信息和偏好。
输出 JSON 数组，每个对象包含以下键：

- content: 事实/偏好的简洁表述
- memory_type: 以下之一：
    user_profile（身份信息：姓名、年龄、地点、职业、语言）
    agent_self（Agent 自身的身份、名称、人格）
    preference（喜好、厌恶、习惯、交互风格）
    project_decision（技术栈、项目配置、架构决策）
    policy（规则、约束、要求）
    procedural（用户建立的工作流、流程、操作指南）
    episodic（值得记住的一次性事件或对话）
- memory_key: 用于去重的稳定键，规范见下方『记忆键规范』，使用其中列出的键
- confidence: 0.0-1.0（你对这是持久事实的确定程度）
- source_span: 用户消息中包含该事实的原始句子或短语

记忆键规范：
""" + MEMORY_KEY_GUIDE + """

重要规则：
- 只提取用户明确陈述的信息，不要推断或猜测
- 身份类记忆的 content 必须是【纯值】，不要带主语或整句（如"以后叫我博士" → content="博士"）
- "我的代码报错了""今天好累" 等暂时性情况不要提取
- 如果没有任何可提取的内容，返回空数组 []

对话内容：
User: {user_message}
Assistant: {reply}

只输出有效的 JSON，不要 markdown 标记，不要解释："""


# ---------------------------------------------------------------------------
# MemoryExtractor - 记忆提取器类
# ---------------------------------------------------------------------------

class MemoryExtractor:
    """使用 LLM 从对话中提取结构化记忆。

    接收用户消息和助手回复，调用 LLM 进行分析，
    返回结构化的 ExtractedMemory 列表。
    """

    def __init__(self, llm_client: LLMClient):
        """初始化记忆提取器。

        Args:
            llm_client: LLM 客户端实例，用于调用大模型。
        """
        self._llm_client: LLMClient = llm_client

    def extract(
        self, user_message: str, reply: str, trace_id: str | None = None
    ) -> list[ExtractedMemory]:
        """从用户消息和助手回复中提取记忆。

        Args:
            user_message: 用户发送的原始消息。
            reply: 助手生成的回复内容。
            trace_id: 可选的追踪 ID，用于链路跟踪。

        Returns:
            ExtractedMemory 列表，如果没有可提取的记忆则返回空列表。
        """
        prompt = EXTRACT_PROMPT.format(user_message=user_message, reply=reply)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        try:
            response: LLMResponse = self._llm_client.chat(
                messages, trace_id=trace_id, temperature=0.1
            )
        except Exception:
            logger.exception("记忆提取LLM调用失败: trace_id=%s", trace_id)
            raise
        return self._parse(response.content)

    def _parse(self, raw: str) -> list[ExtractedMemory]:
        """解析 LLM 原始输出为 ExtractedMemory 列表。

        处理可能的 markdown 代码块包装，并对每一条尝试构造数据模型。
        解析失败时静默跳过并返回空列表。

        Args:
            raw: LLM 返回的原始文本。

        Returns:
            解析后的 ExtractedMemory 列表。
        """
        try:
            text = raw.strip()
            if not text:
                return []
            # 去除可能的 markdown 代码块标记
            for fence in ("```json", "```"):
                if text.startswith(fence):
                    text = text[len(fence):].strip()
                if text.endswith("```"):
                    text = text[:-3].strip()
            if not text:
                return []
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                repaired = repair_json(text)
                data = json.loads(repaired)
            if not isinstance(data, list):
                return []
            results: list[ExtractedMemory] = []
            for item in data:
                try:
                    results.append(ExtractedMemory(
                        content=item.get("content", ""),
                        memory_type=item.get("memory_type", "fact"),
                        memory_key=item.get("memory_key", ""),
                        confidence=item.get("confidence", 0.5),
                        source_span=item.get("source_span", ""),
                        durable=item.get("durable", True),
                    ))
                except Exception:
                    logger.warning("单条记忆解析失败", exc_info=True)
                    continue
            return results
        except (json.JSONDecodeError, ValueError):
            logger.warning("记忆提取JSON解析失败", exc_info=True)
            return []
