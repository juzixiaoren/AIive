"""测试 MemoryGate（记忆门控）模块。

MemoryGate 负责根据用户消息内容判断是否应该触发记忆写入。
包含关键词匹配、弱推理过滤等规则。
"""

from aiive.memory.memory_gate import MemoryGate


class TestMemoryGate:
    """测试 MemoryGate 的关键词触发和候选过滤逻辑。"""

    def setup_method(self):
        self.gate = MemoryGate()

    def test_remember_keyword_triggers_active(self):
        """包含"记住"关键词的消息应触发 active 状态。"""
        assert self.gate.decide("用户喜欢咖啡", "记住我喜欢咖啡") == "active"

    def test_call_me_triggers_active(self):
        """包含"叫我"关键词的消息应触发 active 状态。"""
        assert self.gate.decide("用户叫小明", "以后叫我小明") == "active"

    def test_from_now_on_triggers_active(self):
        """包含"从现在起"关键词的消息应触发 active 状态。"""
        assert self.gate.decide("用户偏好", "从现在起我每天8点起床") == "active"

    def test_preference_pattern_triggers_active(self):
        """偏好模式（如"我喜欢"）应触发 active 状态。"""
        assert self.gate.decide("用户喜欢跑步", "我喜欢跑步") == "active"

    def test_i_dont_like_triggers_active(self):
        """不喜欢/偏好模式（如"我不喜欢"）应触发 active 状态。"""
        assert self.gate.decide("用户不喜欢早起", "我不喜欢早起") == "active"

    def test_casual_chat_stays_candidate(self):
        """普通闲聊消息应保持 candidate 状态，不直接写入。"""
        assert self.gate.decide("用户提到了天气", "今天天气不错") == "candidate"

    def test_weak_inference_stays_candidate(self):
        """弱推理消息（如"I'm learning"类）应保持 candidate 状态。"""
        assert self.gate.decide("用户可能喜欢编程", "我最近在学Python") == "candidate"
