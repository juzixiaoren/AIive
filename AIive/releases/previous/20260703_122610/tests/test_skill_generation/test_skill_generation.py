"""
Skill生成能力测试

测试Agent是否能添加skill（创建能力目录和注册表条目）
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

from core.file_store import FileStore
from core.capability_router import CapabilityRouter
from core.update_executor import UpdateExecutor


class TestSkillGeneration(unittest.TestCase):
    """Skill生成能力测试类"""
    
    def setUp(self):
        """测试前准备"""
        # 创建临时目录
        self.test_dir = tempfile.mkdtemp()
        self.project_root = Path(self.test_dir)
        
        # 创建文件存储器
        self.file_store = FileStore(self.project_root)
        
        # 创建必要的目录结构
        self._create_project_structure()
        
        # 创建能力路由器
        self.capability_router = CapabilityRouter(self.file_store)
        
        # 创建更新执行器
        self.update_executor = UpdateExecutor(str(self.project_root))
    
    def tearDown(self):
        """测试后清理"""
        # 删除临时目录
        shutil.rmtree(self.test_dir)
    
    def _create_project_structure(self):
        """创建项目结构"""
        # 创建必要目录
        necessary_dirs = [
            "capabilities",
            "capabilities/local_tools",
            "capabilities/mcp",
            "capabilities/workflows",
            "capabilities/ui_components",
            "capabilities/dormant",
            "capabilities/deprecated"
        ]
        
        for dir_path in necessary_dirs:
            (self.project_root / dir_path).mkdir(parents=True, exist_ok=True)
        
        # 创建初始registry.md
        registry_content = """# 能力注册表

本文件记录Agent的所有能力。

## 能力状态

- **active**：当前可用，并可参与能力路由
- **dormant**：能力存在，但默认不主动唤醒
- **deprecated**：不推荐使用，保留历史记录
- **candidate**：用户提出或Agent规划中的候选能力，尚未完成

## 能力列表

"""
        (self.project_root / "capabilities" / "registry.md").write_text(registry_content, encoding='utf-8')
    
    def test_add_candidate_capability(self):
        """测试添加候选能力"""
        # 模拟用户输入
        user_input = "你给自己加一个星巴克菜单查询工具，先做 skeleton，不需要真实联网。"
        
        # 使用capability_router添加能力
        result = self.capability_router.add_candidate_capability(user_input, "starbucks_menu_query")
        
        # 验证结果
        self.assertTrue(result["success"])
        self.assertIn("starbucks_menu_query", result["capability_name"])
        
        # 验证registry.md已更新
        registry_content = (self.project_root / "capabilities" / "registry.md").read_text(encoding='utf-8')
        self.assertIn("### starbucks_menu_query", registry_content)
        self.assertIn("Status: candidate", registry_content)
    
    def test_skill_generation_with_directory_creation(self):
        """测试skill生成包括目录创建"""
        # 这个测试需要补充功能：创建能力目录和README
        # 当前capability_router只更新registry，不创建目录
        # 我们需要扩展它来支持完整的skill生成
        
        # 首先，测试当前的registry更新
        user_input = "给自己加一个天气查询工具"
        result = self.capability_router.add_candidate_capability(user_input, "weather_query")
        
        self.assertTrue(result["success"])
        
        # 现在测试是否应该创建目录
        # 根据文档，skill生成应该包括：
        # 1. 创建能力目录
        # 2. 生成README或tool spec
        # 3. 生成skeleton工具文件
        # 4. 更新registry
        
        # 目前只有registry更新，缺少其他部分
        # 这表明需要补充功能
    
    def test_capability_router_name_extraction(self):
        """测试能力名称提取"""
        # 测试各种输入格式
        test_cases = [
            ("加一个天气查询工具", "天气查询"),
            ("加一个汇率查询", "汇率查询"),
            ("给自己加一个翻译功能", "翻译功能"),
            ("写个工具处理PDF文件", "处理PDF文件"),
            ("加一个数据库查询MCP", "数据库查询"),
        ]
        
        for user_input, expected_name in test_cases:
            with self.subTest(user_input=user_input):
                result = self.capability_router._extract_capability_name(user_input)
                # 清理名称以匹配预期
                cleaned_name = result.replace('_', ' ').strip()
                self.assertIn(expected_name, cleaned_name)
    
    def test_capability_status_update(self):
        """测试能力状态更新"""
        # 先添加一个能力
        self.capability_router.add_candidate_capability("测试能力", "test_capability")
        
        # 更新状态为active
        result = self.capability_router.update_capability_status("test_capability", "active")
        self.assertTrue(result["success"])
        
        # 验证状态已更新
        registry_content = (self.project_root / "capabilities" / "registry.md").read_text(encoding='utf-8')
        self.assertIn("Status: active", registry_content)
    
    def test_update_executor_operations(self):
        """测试更新执行器操作"""
        # 测试创建目录操作
        operations = [
            {
                "type": "create_directory",
                "path": "capabilities/local_tools/starbucks_menu_query"
            }
        ]
        
        result = self.update_executor.execute_operations(operations)
        self.assertTrue(result["success"])
        
        # 验证目录已创建
        capability_dir = self.project_root / "capabilities" / "local_tools" / "starbucks_menu_query"
        self.assertTrue(capability_dir.exists())
    
    def test_update_executor_create_file(self):
        """测试更新执行器创建文件"""
        # 先创建目录
        capability_dir = self.project_root / "capabilities" / "local_tools" / "starbucks_menu_query"
        capability_dir.mkdir(parents=True, exist_ok=True)
        
        # 测试创建文件操作
        operations = [
            {
                "type": "create_file",
                "path": "capabilities/local_tools/starbucks_menu_query/README.md",
                "content": """# 星巴克菜单查询工具

## 简介
这是一个查询星巴克菜单的工具。

## 功能
- 查询咖啡种类
- 查询价格
- 查询营养成分

## 状态
Skeleton - 尚未实现
"""
            }
        ]
        
        result = self.update_executor.execute_operations(operations)
        self.assertTrue(result["success"])
        
        # 验证文件已创建
        readme_path = capability_dir / "README.md"
        self.assertTrue(readme_path.exists())
        
        # 验证文件内容
        content = readme_path.read_text(encoding='utf-8')
        self.assertIn("星巴克菜单查询工具", content)


if __name__ == '__main__':
    unittest.main()