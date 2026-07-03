"""
Memory Hit机制测试

测试Agent的记忆hit机制是否正常工作
"""

import unittest
import tempfile
import shutil
import json
from pathlib import Path

# 添加项目根目录到Python路径
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from core.memory_hit_manager import MemoryHitManager


class TestMemoryHit(unittest.TestCase):
    """Memory Hit机制测试类"""
    
    def setUp(self):
        """测试前准备"""
        # 创建临时目录
        self.test_dir = tempfile.mkdtemp()
        self.project_root = Path(self.test_dir)
        
        # 创建MemoryHitManager
        self.hit_manager = MemoryHitManager(str(self.project_root))
    
    def tearDown(self):
        """测试后清理"""
        # 删除临时目录
        shutil.rmtree(self.test_dir)
    
    def test_record_hit(self):
        """测试记录hit"""
        memory_path = "memory/preferences/coffee.md"
        
        result = self.hit_manager.record_hit(memory_path)
        
        self.assertTrue(result["success"])
        self.assertEqual(result["hit_count"], 1)
        
        # 验证文件已创建
        self.assertTrue(self.hit_manager.hits_file.exists())
        
        # 验证数据正确
        with open(self.hit_manager.hits_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            self.assertIn(memory_path, data)
            self.assertEqual(data[memory_path]["hit_count"], 1)
    
    def test_multiple_hits(self):
        """测试多次hit"""
        memory_path = "memory/preferences/coffee.md"
        
        # 记录3次hit
        for _ in range(3):
            self.hit_manager.record_hit(memory_path)
        
        # 验证hit_count为3
        hit_count = self.hit_manager.get_hit_count(memory_path)
        self.assertEqual(hit_count, 3)
    
    def test_get_hit_count_nonexistent(self):
        """测试获取不存在的记忆hit count"""
        memory_path = "memory/preferences/nonexistent.md"
        
        hit_count = self.hit_manager.get_hit_count(memory_path)
        self.assertEqual(hit_count, 0)
    
    def test_get_hit_info(self):
        """测试获取hit信息"""
        memory_path = "memory/preferences/coffee.md"
        
        # 先记录一个hit
        self.hit_manager.record_hit(memory_path)
        
        # 获取信息
        result = self.hit_manager.get_hit_info(memory_path)
        
        self.assertTrue(result["success"])
        self.assertEqual(result["info"]["hit_count"], 1)
        self.assertIsNotNone(result["info"]["last_hit"])
    
    def test_get_all_hits(self):
        """测试获取所有hit记录"""
        # 记录多个记忆的hit
        self.hit_manager.record_hit("memory/preferences/coffee.md")
        self.hit_manager.record_hit("memory/preferences/general.md")
        self.hit_manager.record_hit("memory/events/user_requested_memory.md")
        
        result = self.hit_manager.get_all_hits()
        
        self.assertTrue(result["success"])
        self.assertEqual(result["total_memories"], 3)
    
    def test_get_top_hits(self):
        """测试获取top hit记忆"""
        # 记录不同次数的hit
        for _ in range(5):
            self.hit_manager.record_hit("memory/preferences/coffee.md")
        for _ in range(3):
            self.hit_manager.record_hit("memory/preferences/general.md")
        self.hit_manager.record_hit("memory/events/user_requested_memory.md")
        
        result = self.hit_manager.get_top_hits(limit=2)
        
        self.assertTrue(result["success"])
        self.assertEqual(len(result["top_hits"]), 2)
        self.assertEqual(result["top_hits"][0]["path"], "memory/preferences/coffee.md")
        self.assertEqual(result["top_hits"][0]["hit_count"], 5)
    
    def test_read_memory_with_hit(self):
        """测试读取记忆并记录hit"""
        memory_path = "memory/preferences/coffee.md"
        
        result = self.hit_manager.read_memory_with_hit(memory_path)
        
        self.assertTrue(result["success"])
        self.assertEqual(result["hit_count"], 1)
    
    def test_hit_data_persistence(self):
        """测试hit数据持久化"""
        memory_path = "memory/preferences/coffee.md"
        
        # 记录hit
        self.hit_manager.record_hit(memory_path)
        
        # 创建新的MemoryHitManager实例（模拟重新加载）
        new_hit_manager = MemoryHitManager(str(self.project_root))
        
        # 验证数据被正确加载
        hit_count = new_hit_manager.get_hit_count(memory_path)
        self.assertEqual(hit_count, 1)
    
    def test_multiple_memories(self):
        """测试多个记忆的hit统计"""
        memories = [
            "memory/preferences/coffee.md",
            "memory/preferences/general.md",
            "memory/events/user_requested_memory.md",
            "memory/feedback/interaction_errors.md"
        ]
        
        # 为每个记忆记录不同次数的hit
        for i, memory in enumerate(memories):
            for _ in range(i + 1):
                self.hit_manager.record_hit(memory)
        
        # 验证每个记忆的hit count
        for i, memory in enumerate(memories):
            hit_count = self.hit_manager.get_hit_count(memory)
            self.assertEqual(hit_count, i + 1)


if __name__ == '__main__':
    unittest.main()