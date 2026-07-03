"""
Intent Classifier模块

实现基于规则的intent分类器。
"""

import re


class IntentClassifier:
    """意图分类器"""
    
    # 意图类型
    INTENT_TYPES = [
        "ordinary_chat",
        "preference_update",
        "persona_update",
        "listen_policy_update",
        "memory_update",
        "new_capability_request",
        "runtime_change_request",
        "bug_report",
        "documentation_update",
        "self_development_request"
    ]
    
    # 关键词规则
    KEYWORD_RULES = {
        "bug_report": [
            r"你刚才错了", r"没反应", r"误会", r"不是叫你",
            r"刚才那句话", r"你怎么没反应", r"你误会了",
            r"我刚才是在和别人说话", r"不是在叫你"
        ],
        "memory_update": [
            r"记住", r"记录一下", r"忘记", r"以后别忘了", r"忘掉",
            r"以后记得", r"记得提醒"
        ],
        "preference_update": [
            r"我喜欢", r"我不喜欢", r"我爱", r"我不爱", r"优先", r"偏好",
            r"以后.*推荐", r"以后.*优先", r"以后.*考虑"
        ],
        "persona_update": [
            r"以后你说话", r"你别", r"你要更", r"风格", r"别这么像",
            r"讨论.*时.*大胆", r"不要太保守", r"先展开可能性",
            r"不要太早保守化", r"大胆一点"
        ],
        "listen_policy_update": [
            r"叫你", r"唤醒", r"你才应", r"别插嘴", r"skip",
            r"以后我叫你", r"和别人聊天时", r"别主动回应",
            r"戴耳机", r"提醒我", r"你可以直接记"
        ],
        "runtime_change_request": [
            r"hit 机制", r"sleep 功能", r"索引", r"记忆整理", r"运行机制",
            r"给记忆加", r"给自己加.*功能", r"每晚.*整理", r"优化.*路由"
        ],
        "new_capability_request": [
            r"加一个工具", r"MCP", r"新增能力", r"点单", r"查询工具",
            r"给自己加", r"写个工具", r"加一个.*工具",
            r"加一个.*卡片", r"加一个.*功能"
        ],
        "documentation_update": [
            r"更新文档", r"写到文档", r"写进架构文档", r"更新一下宪法"
        ],
        "self_development_request": [
            r"继续完善你自己", r"自我开发", r"继续迭代", r"继续开发"
        ]
    }
    
    def classify(self, user_input):
        """
        分类用户输入
        
        Args:
            user_input: 用户输入文本
            
        Returns:
            str: 意图类型
        """
        if not user_input or not user_input.strip():
            return "ordinary_chat"
        
        user_input = user_input.strip()
        
        # 检查每个意图类型的关键词规则
        for intent_type, patterns in self.KEYWORD_RULES.items():
            for pattern in patterns:
                if re.search(pattern, user_input):
                    return intent_type
        
        # 默认返回普通聊天
        return "ordinary_chat"
    
    def get_intent_description(self, intent_type):
        """
        获取意图描述
        
        Args:
            intent_type: 意图类型
            
        Returns:
            str: 意图描述
        """
        descriptions = {
            "ordinary_chat": "普通聊天或普通问答",
            "preference_update": "用户偏好更新",
            "persona_update": "人格和表达风格调整",
            "listen_policy_update": "呼叫规则、响应规则、skip规则",
            "memory_update": "明确要求记忆或遗忘",
            "new_capability_request": "新增工具、MCP、工作流、UI组件",
            "runtime_change_request": "修改运行机制，例如hit、sleep、索引、调度",
            "bug_report": "用户报告Agent行为不对",
            "documentation_update": "用户要求更新文档",
            "self_development_request": "让Agent规划或继续自我开发"
        }
        
        return descriptions.get(intent_type, "未知意图")
    
    def get_confidence(self, user_input, intent_type):
        """
        获取分类置信度
        
        Args:
            user_input: 用户输入文本
            intent_type: 意图类型
            
        Returns:
            float: 置信度（0-1）
        """
        if intent_type == "ordinary_chat":
            return 0.5
        
        # 计算匹配的关键词数量
        patterns = self.KEYWORD_RULES.get(intent_type, [])
        match_count = 0
        
        for pattern in patterns:
            if re.search(pattern, user_input):
                match_count += 1
        
        if not patterns:
            return 0.5
        
        # 计算置信度
        confidence = min(0.5 + (match_count / len(patterns)) * 0.5, 1.0)
        
        return confidence