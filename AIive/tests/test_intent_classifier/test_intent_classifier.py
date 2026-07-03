"""
意图分类器测试
"""

import unittest

# 添加项目根目录到Python路径
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.intent_classifier import IntentClassifier


class TestIntentClassifier(unittest.TestCase):
    """意图分类器测试类"""
    
    def setUp(self):
        """测试前准备"""
        self.classifier = IntentClassifier()
    
    def test_ordinary_chat(self):
        """测试普通聊天分类"""
        test_cases = [
            "这个想法怎么样？",
            "帮我分析一下这个架构。",
            "你觉得这样可行吗？",
            "今天天气怎么样？",
            "你好"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "ordinary_chat", f"'{test_case}' 应该分类为 ordinary_chat")
    
    def test_preference_update(self):
        """测试偏好更新分类"""
        test_cases = [
            "我不爱喝瑞幸，我爱喝星巴克。",
            "以后咖啡优先考虑星巴克。",
            "我不喜欢太甜的东西。",
            "我喜欢简洁的界面。",
            "以后推荐优先考虑苹果产品。"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "preference_update", f"'{test_case}' 应该分类为 preference_update")
    
    def test_persona_update(self):
        """测试人格更新分类"""
        test_cases = [
            "你以后别这么像产品经理。",
            "你讨论产品设想时先大胆一点。",
            "不要太早保守化。",
            "以后你说话直接一点。",
            "你要更幽默一些。"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "persona_update", f"'{test_case}' 应该分类为 persona_update")
    
    def test_listen_policy_update(self):
        """测试listen policy更新分类"""
        test_cases = [
            "以后我叫你大李的时候你才应。",
            "和别人聊天时别插嘴。",
            "我一个人戴耳机走路时，说提醒我，你可以直接记。",
            "叫你小李的时候才回应。",
            "别主动回应。"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "listen_policy_update", f"'{test_case}' 应该分类为 listen_policy_update")
    
    def test_memory_update(self):
        """测试记忆更新分类"""
        test_cases = [
            "记住这个。",
            "以后别忘了。",
            "忘掉刚才那个偏好。",
            "记录一下这个想法。",
            "以后记得提醒我。"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "memory_update", f"'{test_case}' 应该分类为 memory_update")
    
    def test_new_capability_request(self):
        """测试新能力请求分类"""
        test_cases = [
            "你给自己加一个星巴克菜单查询工具。",
            "你写个工具帮我整理聊天记录。",
            "你加一个手机端动态卡片。",
            "加一个天气查询工具。",
            "给自己加一个日程管理MCP。"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "new_capability_request", f"'{test_case}' 应该分类为 new_capability_request")
    
    def test_runtime_change_request(self):
        """测试运行机制修改请求分类"""
        test_cases = [
            "你给记忆加一个 hit 机制。",
            "你给自己加一个 sleep 功能。",
            "你以后每晚整理一次记忆。",
            "你优化一下能力路由。",
            "给记忆加一个索引功能。"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "runtime_change_request", f"'{test_case}' 应该分类为 runtime_change_request")
    
    def test_bug_report(self):
        """测试bug报告分类"""
        test_cases = [
            "刚才那句话我是跟你说的，你怎么没反应？",
            "我刚才是在和别人说话，不是叫你。",
            "你误会了。",
            "你刚才错了。",
            "你怎么没反应？"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "bug_report", f"'{test_case}' 应该分类为 bug_report")
    
    def test_documentation_update(self):
        """测试文档更新分类"""
        test_cases = [
            "把这个写进架构文档。",
            "更新一下宪法文档。",
            "更新文档。",
            "写到文档里。"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "documentation_update", f"'{test_case}' 应该分类为 documentation_update")
    
    def test_self_development_request(self):
        """测试自我开发请求分类"""
        test_cases = [
            "继续完善你自己。",
            "自我开发。",
            "继续迭代。",
            "继续开发。"
        ]
        
        for test_case in test_cases:
            result = self.classifier.classify(test_case)
            self.assertEqual(result, "self_development_request", f"'{test_case}' 应该分类为 self_development_request")
    
    def test_confidence(self):
        """测试置信度"""
        # 测试偏好更新置信度
        confidence = self.classifier.get_confidence("我不爱喝瑞幸，我爱喝星巴克。", "preference_update")
        self.assertGreater(confidence, 0.5)
        self.assertLessEqual(confidence, 1.0)
        
        # 测试普通聊天置信度
        confidence = self.classifier.get_confidence("你好", "ordinary_chat")
        self.assertEqual(confidence, 0.5)
    
    def test_intent_description(self):
        """测试意图描述"""
        description = self.classifier.get_intent_description("preference_update")
        self.assertEqual(description, "用户偏好更新")
        
        description = self.classifier.get_intent_description("unknown")
        self.assertEqual(description, "未知意图")


if __name__ == "__main__":
    unittest.main()