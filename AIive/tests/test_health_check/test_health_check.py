"""
健康检查测试
"""

import unittest
import tempfile
import shutil
from pathlib import Path

# 添加项目根目录到Python路径
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from kernel.health_check import HealthCheck


class TestHealthCheck(unittest.TestCase):
    """健康检查测试类"""
    
    def setUp(self):
        """测试前准备"""
        # 创建临时目录
        self.test_dir = tempfile.mkdtemp()
        self.project_root = Path(self.test_dir)
        
        # 创建必要的目录结构
        self._create_project_structure()
        
        # 创建健康检查器
        self.health_check = HealthCheck(self.project_root)
    
    def tearDown(self):
        """测试后清理"""
        # 删除临时目录
        shutil.rmtree(self.test_dir)
    
    def _create_project_structure(self):
        """创建项目结构"""
        # 创建必要目录
        necessary_dirs = [
            "docs",
            "core",
            "mind",
            "memory/preferences",
            "memory/feedback",
            "memory/events",
            "memory/summaries",
            "memory/archive",
            "memory/index",
            "capabilities",
            "self_development",
            "kernel",
            "releases/current",
            "releases/candidate",
            "releases/previous",
            "logs",
            "tests"
        ]
        
        for dir_path in necessary_dirs:
            (self.project_root / dir_path).mkdir(parents=True, exist_ok=True)
        
        # 创建必要文件
        necessary_files = [
            "main.py",
            "README.md",
            "mind/system_prompt.md",
            "mind/persona.md",
            "mind/user_model.md",
            "mind/listen_policy.md",
            "mind/goals.md",
            "mind/daily_context.md",
            "mind/self_model.md",
            "capabilities/registry.md",
            "self_development/issues.md",
            "self_development/ideas.md",
            "self_development/changelog.md"
        ]
        
        for file_path in necessary_files:
            (self.project_root / file_path).touch()
    
    def test_run_check_pass(self):
        """测试健康检查通过"""
        result = self.health_check.run_check()
        
        self.assertTrue(result["pass"])
        self.assertEqual(len(result["reasons"]), 0)
    
    def test_missing_directory(self):
        """测试缺少目录"""
        # 删除一个必要目录
        shutil.rmtree(self.project_root / "docs")
        
        result = self.health_check.run_check()
        
        self.assertFalse(result["pass"])
        self.assertIn("目录不存在: docs", result["reasons"])
    
    def test_missing_file(self):
        """测试缺少文件"""
        # 删除一个必要文件
        os.remove(self.project_root / "main.py")
        
        result = self.health_check.run_check()
        
        self.assertFalse(result["pass"])
        self.assertIn("文件不存在: main.py", result["reasons"])
    
    def test_missing_mind_file(self):
        """测试缺少mind文件"""
        # 删除一个mind文件
        os.remove(self.project_root / "mind/persona.md")
        
        result = self.health_check.run_check()
        
        self.assertFalse(result["pass"])
        self.assertIn("mind文件不存在: persona.md", result["reasons"])
    
    def test_missing_memory_directory(self):
        """测试缺少memory目录"""
        # 删除一个memory子目录
        shutil.rmtree(self.project_root / "memory/preferences")
        
        result = self.health_check.run_check()
        
        self.assertFalse(result["pass"])
        self.assertIn("memory目录不存在: preferences", result["reasons"])
    
    def test_missing_capabilities_registry(self):
        """测试缺少capabilities registry"""
        # 删除registry文件
        os.remove(self.project_root / "capabilities/registry.md")
        
        result = self.health_check.run_check()
        
        self.assertFalse(result["pass"])
        self.assertIn("capabilities/registry.md不存在", result["reasons"])
    
    def test_missing_self_development_file(self):
        """测试缺少self_development文件"""
        # 删除一个self_development文件
        os.remove(self.project_root / "self_development/issues.md")
        
        result = self.health_check.run_check()
        
        self.assertFalse(result["pass"])
        self.assertIn("self_development文件不存在: issues.md", result["reasons"])
    
    def test_logs_not_writable(self):
        """测试logs目录不可写"""
        # 这个测试需要特殊处理，因为我们需要模拟不可写的情况
        # 在实际测试中，可能需要使用mock
        pass


if __name__ == "__main__":
    unittest.main()