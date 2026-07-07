ACTIVE_KEYWORDS = [
    "记住", "记下", "别忘了", "以后", "从现在起", "叫我", "称呼我",
    "remember", "don't forget", "from now on", "call me",
]

PREFERENCE_PATTERNS = [
    "我喜欢", "我不喜欢", "我习惯", "我不习惯", "我的",
    "I like", "I don't like", "I prefer", "I hate", "my",
]

ROUTINE_PATTERNS = [
    "每天", "每周", "工作日", "周末", "早上", "晚上", "上午", "下午",
    "every day", "every week", "every morning", "every evening",
    "weekday", "weekend", "daily", "weekly",
]


class MemoryGate:
    def decide(self, content: str, user_message: str) -> str:
        """Returns 'active', 'candidate', or 'reject'."""
        msg_lower = user_message.lower()

        for kw in ACTIVE_KEYWORDS:
            if kw.lower() in msg_lower:
                return "active"

        for pat in PREFERENCE_PATTERNS:
            if pat.lower() in msg_lower:
                return "active"

        for pat in ROUTINE_PATTERNS:
            if pat.lower() in msg_lower:
                return "active"

        return "candidate"
