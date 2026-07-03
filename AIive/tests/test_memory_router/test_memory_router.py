"""
记忆路由器测试
"""

import unittest
import tempfile
import shutil
from pathlib import Path

# 添加项目根目录到Python路径
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.file_store import FileStore
from core.memory_router import MemoryRouter


class TestMemoryRouter(unittest.TestCase):
    """记忆路由器测试类"""
    
    def setUp(self):
        """测试前准备"""
        # 创建临时目录
        self.test_dir = tempfile.mkdtemp()
        self.project_root = Path(self.test_dir)
        
        # 创建必要的目录结构
        self._create_project_structure()
        
        # 创建文件存储器和记忆路由器
        self.file_store = FileStore(self.project_root)
        self.memory_router = MemoryRouter(self.file_store)
    
    def tearDown(self):
        """测试后清理"""
        # 删除临时目录
        shutil.rmtree(self.test_dir)
    
    def _create_project_structure(self):
        """创建项目结构"""
        # 创建必要目录
        necessary_dirs = [
            "memory/preferences",
            "memory/feedback",
            "memory/events"
        ]
        
        for dir_path in necessary_dirs:
            (self.project_root / dir_path).mkdir(parents=True, exist_ok=True)
    
    def test_coffee_preference_update(self):
        """测试咖啡偏好更新"""
        user_input = "我不爱喝瑞幸，我爱喝星巴克。"
        
        result = self.memory_router.route_memory(user_input, "preference_update")
        
        self.assertTrue(result["success"])
        self.assertIn("咖啡偏好", result["message"])
        self.assertEqual(result["file_path"], "memory/preferences/coffee.md")
        
        # 检查文件是否被创建
        coffee_file = self.project_root / "memory/preferences/coffee.md"
        self.assertTrue(coffee_file.exists())
        
        # 检查文件内容
        content = self.file_store.read_markdown("memory/preferences/coffee.md")
        self.assertIn("星巴克", content)
        self.assertIn("瑞幸", content)
    
    def test_general_preference_update(self):
        """测试通用偏好更新"""
        user_input = "我喜欢简洁的界面。"
        
        result = self.memory_router.route_memory(user_input, "preference_update")
        
        self.assertTrue(result["success"])
        self.assertIn("通用偏好", result["message"])
        self.assertEqual(result["file_path"], "memory/preferences/general.md")
        
        # 检查文件是否被创建
        general_file = self.project_root / "memory/preferences/general.md"
        self.assertTrue(general_file.exists())
        
        # 检查文件内容
        content = self.file_store.read_markdown("memory/preferences/general.md")
        self.assertIn("简洁的界面", content)
    
    def test_memory_update(self):
        """测试记忆更新"""
        user_input = "记住明天下午3点开会。"
        
        result = self.memory_router.route_memory(user_input, "memory_update")
        
        self.assertTrue(result["success"])
        self.assertIn("事件记忆", result["message"])
        self.assertEqual(result["file_path"], "memory/events/user_requested_memory.md")
        
        # 检查文件是否被创建
        memory_file = self.project_root / "memory/events/user_requested_memory.md"
        self.assertTrue(memory_file.exists())
        
        # 检查文件内容
        content = self.file_store.read_markdown("memory/events/user_requested_memory.md")
        self.assertIn("明天下午3点开会", content)
    
    def test_bug_report(self):
        """测试bug报告"""
        user_input = "刚才那句话我是跟你说的，你怎么没反应？"
        
        result = self.memory_router.route_memory(user_input, "bug_report")
        
        self.assertTrue(result["success"])
        self.assertIn("反馈记忆", result["message"])
        self.assertEqual(result["file_path"], "memory/feedback/interaction_errors.md")
        
        # 检查文件是否被创建
        feedback_file = self.project_root / "memory/feedback/interaction_errors.md"
        self.assertTrue(feedback_file.exists())
        
        # 检查文件内容
        content = self.file_store.read_markdown("memory/feedback/interaction_errors.md")
        self.assertIn("刚才那句话", content)
    
    def test_unsupported_intent(self):
        """测试不支持的意图类型"""
        user_input = "普通聊天"
        
        result = self.memory_router.route_memory(user_input, "ordinary_chat")
        
        self.assertFalse(result["success"])
        self.assertIn("不支持的记忆类型", result["message"])
    
    def test_extract_preference_content(self):
        """测试提取偏好内容"""
        # 测试"不爱X，爱Y"格式
        user_input = "我不爱喝瑞幸，我爱喝星巴克。"
        content = self.memory_router._extract_preference_content(user_input)
        
        self.assertIn("不爱喝瑞幸", content)
        self.assertIn("爱喝星巴克", content)
    
    def test_extract_memory_content(self):
        """测试提取记忆内容"""
        # 测试"记住"格式
        user_input = "记住明天下午3点开会。"
        content = self.memory_router._extract_memory_content(user_input)
        
        self.assertIn("明天下午3点开会", content)
        self.assertNotIn("记住", content)
    
    def test_extract_bug_content(self):
        """测试提取bug内容"""
        user_input = "刚才那句话我是跟你说的，你怎么没反应？"
        content = self.memory_router._extract_bug_content(user_input)
        
        self.assertIn("刚才那句话", content)
        self.assertIn("用户报告问题", content)


if __name__ == "__main__":
    unittest.main()