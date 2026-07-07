from aiive.memory.memory_gate import MemoryGate


class TestMemoryGate:
    def setup_method(self):
        self.gate = MemoryGate()

    def test_remember_keyword_triggers_active(self):
        assert self.gate.decide("用户喜欢咖啡", "记住我喜欢咖啡") == "active"

    def test_call_me_triggers_active(self):
        assert self.gate.decide("用户叫小明", "以后叫我小明") == "active"

    def test_from_now_on_triggers_active(self):
        assert self.gate.decide("用户偏好", "从现在起我每天8点起床") == "active"

    def test_preference_pattern_triggers_active(self):
        assert self.gate.decide("用户喜欢跑步", "我喜欢跑步") == "active"

    def test_i_dont_like_triggers_active(self):
        assert self.gate.decide("用户不喜欢早起", "我不喜欢早起") == "active"

    def test_casual_chat_stays_candidate(self):
        assert self.gate.decide("用户提到了天气", "今天天气不错") == "candidate"

    def test_weak_inference_stays_candidate(self):
        assert self.gate.decide("用户可能喜欢编程", "我最近在学Python") == "candidate"
