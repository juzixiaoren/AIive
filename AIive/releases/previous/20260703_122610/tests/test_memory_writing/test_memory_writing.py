"""
Memory写入能力测试

测试Agent是否能写入memory（记忆写入功能）
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


class TestMemoryWriting(unittest.TestCase):
    """Memory写入能力测试类"""
    
    def setUp(self):
        """测试前准备"""
        # 创建临时目录
        self.test_dir = tempfile.mkdtemp()
        self.project_root = Path(self.test_dir)
        
        # 创建文件存储器
        self.file_store = FileStore(self.project_root)
        
        # 创建必要的目录结构
        self._create_project_structure()
        
        # 创建记忆路由器
        self.memory_router = MemoryRouter(self.file_store)
    
    def tearDown(self):
        """测试后清理"""
        # 删除临时目录
        shutil.rmtree(self.test_dir)
    
    def _create_project_structure(self):
        """创建项目结构"""
        # 创建必要目录
        necessary_dirs = [
            "memory",
            "memory/preferences",
            "memory/feedback",
            "memory/events",
            "memory/summaries",
            "memory/archive",
            "memory/index"
        ]
        
        for dir_path in necessary_dirs:
            (self.project_root / dir_path).mkdir(parents=True, exist_ok=True)
        
        # 创建初始记忆文件
        coffee_content = """# 咖啡偏好

记录用户的咖啡偏好。

"""
        (self.project_root / "memory" / "preferences" / "coffee.md").write_text(coffee_content, encoding='utf-8')
        
        general_content = """# 通用偏好

记录用户的通用偏好。

"""
        (self.project_root / "memory" / "preferences" / "general.md").write_text(general_content, encoding='utf-8')
        
        events_content = """# 用户事件记忆

记录用户明确要求记住的事件。

"""
        (self.project_root / "memory" / "events" / "user_requested_memory.md").write_text(events_content, encoding='utf-8')
        
        feedback_content = """# 交互错误反馈

记录用户报告的Agent行为问题。

"""
        (self.project_root / "memory" / "feedback" / "interaction_errors.md").write_text(feedback_content, encoding='utf-8')
    
    def test_coffee_preference_update(self):
        """测试咖啡偏好更新"""
        user_input = "我不爱喝瑞幸，我爱喝星巴克。"
        
        result = self.memory_router.route_memory(user_input, "preference_update")
        
        self.assertTrue(result["success"])
        self.assertIn("咖啡偏好", result["message"])
        self.assertEqual(result["file_path"], "memory/preferences/coffee.md")
        
        # 验证文件内容
        coffee_content = (self.project_root / "memory" / "preferences" / "coffee.md").read_text(encoding='utf-8')
        self.assertIn("瑞幸", coffee_content)
        self.assertIn("星巴克", coffee_content)
    
    def test_general_preference_update(self):
        """测试通用偏好更新"""
        user_input = "我喜欢红色，不喜欢蓝色。"
        
        result = self.memory_router.route_memory(user_input, "preference_update")
        
        self.assertTrue(result["success"])
        self.assertIn("通用偏好", result["message"])
        self.assertEqual(result["file_path"], "memory/preferences/general.md")
        
        # 验证文件内容
        general_content = (self.project_root / "memory" / "preferences" / "general.md").read_text(encoding='utf-8')
        self.assertIn("红色", general_content)
        self.assertIn("蓝色", general_content)
    
    def test_memory_update(self):
        """测试记忆更新"""
        user_input = "记住我的生日是1990年1月1日。"
        
        result = self.memory_router.route_memory(user_input, "memory_update")
        
        self.assertTrue(result["success"])
        self.assertIn("事件记忆", result["message"])
        self.assertEqual(result["file_path"], "memory/events/user_requested_memory.md")
        
        # 验证文件内容
        events_content = (self.project_root / "memory" / "events" / "user_requested_memory.md").read_text(encoding='utf-8')
        self.assertIn("1990年1月1日", events_content)
    
    def test_bug_report(self):
        """测试bug报告"""
        user_input = "你刚刚回复的时候卡死了，重启后才好。"
        
        result = self.memory_router.route_memory(user_input, "bug_report")
        
        self.assertTrue(result["success"])
        self.assertIn("反馈记忆", result["message"])
        self.assertEqual(result["file_path"], "memory/feedback/interaction_errors.md")
        
        # 验证文件内容
        feedback_content = (self.project_root / "memory" / "feedback" / "interaction_errors.md").read_text(encoding='utf-8')
        self.assertIn("卡死", feedback_content)
    
    def test_unsupported_intent(self):
        """测试不支持的意图类型"""
        user_input = "今天天气真好。"
        
        result = self.memory_router.route_memory(user_input, "unsupported_intent")
        
        self.assertFalse(result["success"])
        self.assertIn("不支持的记忆类型", result["message"])
    
    def test_memory_extraction_functions(self):
        """测试记忆内容提取函数"""
        # 测试偏好内容提取
        preference_content = self.memory_router._extract_preference_content("我不爱喝瑞幸，我爱喝星巴克。")
        self.assertIn("瑞幸", preference_content)
        self.assertIn("星巴克", preference_content)
        
        # 测试记忆内容提取
        memory_content = self.memory_router._extract_memory_content("记住我的生日是1990年1月1日。")
        self.assertIn("1990年1月1日", memory_content)
        
        # 测试bug内容提取
        bug_content = self.memory_router._extract_bug_content("你刚刚回复的时候卡死了。")
        self.assertIn("卡死", bug_content)
    
    def test_memory_file_creation(self):
        """测试记忆文件创建"""
        # 测试当记忆文件不存在时是否能创建
        user_input = "记住我喜欢吃苹果。"
        
        # 删除现有文件
        events_file = self.project_root / "memory" / "events" / "user_requested_memory.md"
        if events_file.exists():
            events_file.unlink()
        
        result = self.memory_router.route_memory(user_input, "memory_update")
        
        self.assertTrue(result["success"])
        self.assertTrue(events_file.exists())
        
        # 验证内容
        content = events_file.read_text(encoding='utf-8')
        self.assertIn("苹果", content)
    
    def test_memory_append_behavior(self):
        """测试记忆追加行为"""
        # 先添加一条记忆
        user_input1 = "记住我喜欢吃苹果。"
        self.memory_router.route_memory(user_input1, "memory_update")
        
        # 再添加一条记忆
        user_input2 = "记住我喜欢吃香蕉。"
        self.memory_router.route_memory(user_input2, "memory_update")
        
        # 验证两条记忆都存在
        events_content = (self.project_root / "memory" / "events" / "user_requested_memory.md").read_text(encoding='utf-8')
        self.assertIn("苹果", events_content)
        self.assertIn("香蕉", events_content)


if __name__ == '__main__':
    unittest.main()