"""
集成测试

验证三个核心验收标准。
"""

import unittest
import tempfile
import shutil
from pathlib import Path

# 添加项目根目录到Python路径
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.agent_loop import AgentLoop


class TestIntegration(unittest.TestCase):
    """集成测试类"""
    
    def setUp(self):
        """测试前准备"""
        # 创建临时目录
        self.test_dir = tempfile.mkdtemp()
        self.project_root = Path(self.test_dir)
        
        # 创建Agent主循环
        self.agent = AgentLoop(self.project_root)
        
        # 检查LLM是否配置
        self.llm_configured = self.agent.llm_client is not None
    
    def tearDown(self):
        """测试后清理"""
        # 删除临时目录
        shutil.rmtree(self.test_dir)
    
    def test_coffee_preference(self):
        """
        核心验收标准1：咖啡偏好写入
        
        输入：我不爱喝瑞幸，我爱喝星巴克。
        期望：真实修改 memory/preferences/coffee.md
        """
        user_input = "我不爱喝瑞幸，我爱喝星巴克。"
        
        # 处理输入
        response = self.agent.process_input(user_input)
        
        if self.llm_configured:
            # LLM配置时，检查正常回复
            self.assertIn("咖啡偏好", response)
        else:
            # LLM未配置时，检查降级回复
            self.assertIn("LLM未配置", response)
        
        # 检查文件是否被修改（降级模式也会创建issue）
        issues_file = self.project_root / "self_development/issues.md"
        self.assertTrue(issues_file.exists(), "issues.md 文件应该被创建")
        
        if self.llm_configured:
            # LLM配置时，检查coffee.md
            coffee_file = self.project_root / "memory/preferences/coffee.md"
            self.assertTrue(coffee_file.exists(), "coffee.md 文件应该被创建")
            
            # 检查文件内容
            content = self.agent.file_store.read_markdown("memory/preferences/coffee.md")
            self.assertIn("星巴克", content, "coffee.md 应该包含星巴克")
            self.assertIn("瑞幸", content, "coffee.md 应该包含瑞幸")
    
    def test_listen_policy_update(self):
        """
        核心验收标准2：listen policy更新
        
        输入：以后我叫你大李的时候你才应，和别人聊天时别插嘴。
        期望：真实修改 mind/listen_policy.md
        """
        user_input = "以后我叫你大李的时候你才应，和别人聊天时别插嘴。"
        
        # 处理输入
        response = self.agent.process_input(user_input)
        
        if self.llm_configured:
            # LLM配置时，检查正常回复
            self.assertIn("交互规则", response)
            
            # 检查文件是否被修改
            listen_policy_file = self.project_root / "mind/listen_policy.md"
            self.assertTrue(listen_policy_file.exists(), "listen_policy.md 文件应该存在")
            
            # 检查文件内容
            content = self.agent.file_store.read_markdown("mind/listen_policy.md")
            self.assertIn("大李", content, "listen_policy.md 应该包含大李")
        else:
            # LLM未配置时，检查降级回复
            self.assertIn("LLM未配置", response)
    
    def test_runtime_change_request(self):
        """
        核心验收标准3：运行机制修改请求
        
        输入：你给记忆加一个 hit 机制。
        期望：真实创建 self_development/issues.md 条目
        """
        user_input = "你给记忆加一个 hit 机制。"
        
        # 处理输入
        response = self.agent.process_input(user_input)
        
        if self.llm_configured:
            # LLM配置时，检查正常回复
            self.assertIn("运行机制修改请求", response)
            self.assertIn("待开发任务", response)
        else:
            # LLM未配置时，检查降级回复
            self.assertIn("LLM未配置", response)
        
        # 检查issues文件是否被修改
        issues_file = self.project_root / "self_development/issues.md"
        self.assertTrue(issues_file.exists(), "issues.md 文件应该存在")
        
        # 检查文件内容
        content = self.agent.file_store.read_markdown("self_development/issues.md")
        self.assertIn("hit", content.lower(), "issues.md 应该包含hit")
    
    def test_agent_loop_integration(self):
        """测试Agent主循环集成"""
        # 测试多个输入
        test_cases = [
            {
                "input": "我不爱喝瑞幸，我爱喝星巴克。",
                "expected_in_response": "咖啡偏好" if self.llm_configured else "LLM未配置",
                "expected_file": "memory/preferences/coffee.md" if self.llm_configured else "self_development/issues.md"
            },
            {
                "input": "以后我叫你大李的时候你才应，和别人聊天时别插嘴。",
                "expected_in_response": "交互规则" if self.llm_configured else "LLM未配置",
                "expected_file": "mind/listen_policy.md" if self.llm_configured else "self_development/issues.md"
            },
            {
                "input": "你给记忆加一个 hit 机制。",
                "expected_in_response": "运行机制修改请求" if self.llm_configured else "LLM未配置",
                "expected_file": "self_development/issues.md"
            }
        ]
        
        for test_case in test_cases:
            # 处理输入
            response = self.agent.process_input(test_case["input"])
            
            # 检查回复
            self.assertIn(test_case["expected_in_response"], response)
            
            # 检查文件是否存在
            file_path = self.project_root / test_case["expected_file"]
            self.assertTrue(file_path.exists(), f"{test_case['expected_file']} 文件应该存在")
    
    def test_multiple_interactions(self):
        """测试多次交互"""
        # 第一次交互
        response1 = self.agent.process_input("我不爱喝瑞幸，我爱喝星巴克。")
        if self.llm_configured:
            self.assertIn("咖啡偏好", response1)
        else:
            self.assertIn("LLM未配置", response1)
        
        # 第二次交互
        response2 = self.agent.process_input("以后我叫你大李的时候你才应，和别人聊天时别插嘴。")
        if self.llm_configured:
            self.assertIn("交互规则", response2)
        else:
            self.assertIn("LLM未配置", response2)
        
        # 第三次交互
        response3 = self.agent.process_input("你给记忆加一个 hit 机制。")
        if self.llm_configured:
            self.assertIn("运行机制修改请求", response3)
        else:
            self.assertIn("LLM未配置", response3)
        
        # 检查所有文件是否都被正确修改
        issues_file = self.project_root / "self_development/issues.md"
        self.assertTrue(issues_file.exists(), "issues.md 文件应该存在")
        
        if self.llm_configured:
            coffee_file = self.project_root / "memory/preferences/coffee.md"
            listen_policy_file = self.project_root / "mind/listen_policy.md"
            
            self.assertTrue(coffee_file.exists(), "coffee.md 文件应该存在")
            self.assertTrue(listen_policy_file.exists(), "listen_policy.md 文件应该存在")


if __name__ == "__main__":
    unittest.main()
