"""
能力注册表测试
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
from core.capability_router import CapabilityRouter


class TestCapabilityRegistry(unittest.TestCase):
    """能力注册表测试类"""
    
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
    
    def tearDown(self):
        """测试后清理"""
        # 删除临时目录
        shutil.rmtree(self.test_dir)
    
    def _create_project_structure(self):
        """创建项目结构"""
        # 创建必要目录
        necessary_dirs = [
            "capabilities"
        ]
        
        for dir_path in necessary_dirs:
            (self.project_root / dir_path).mkdir(parents=True, exist_ok=True)
        
        # 创建registry文件
        registry_content = """# 能力注册表

本文件记录Agent的所有能力。

## 能力状态

- **active**：当前可用，并可参与能力路由
- **dormant**：能力存在，但默认不主动唤醒
- **deprecated**：不推荐使用，保留历史记录
- **candidate**：用户提出或Agent规划中的候选能力，尚未完成

## 能力列表

### 1. 文档读取能力

- Status: active
- Type: local_tool
- Description: 读取docs/下的项目文档
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 启动时、需要理解项目时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: core/file_store.py
- Notes: 基础能力
"""
        
        self.file_store.write_markdown("capabilities/registry.md", registry_content)
    
    def test_add_candidate_capability(self):
        """测试添加候选能力"""
        user_input = "你给自己加一个星巴克菜单查询工具。"
        
        result = self.capability_router.add_candidate_capability(user_input)
        
        self.assertTrue(result["success"])
        self.assertIn("星巴克菜单查询", result["capability_name"])
        self.assertIn("候选能力", result["message"])
        
        # 检查registry是否被更新
        registry_content = self.file_store.read_markdown("capabilities/registry.md")
        self.assertIn("星巴克菜单查询", registry_content)
        self.assertIn("candidate", registry_content)
    
    def test_add_existing_capability(self):
        """测试添加已存在的能力"""
        # 先添加一个能力
        user_input = "你给自己加一个星巴克菜单查询工具。"
        self.capability_router.add_candidate_capability(user_input)
        
        # 再次添加相同的能力
        result = self.capability_router.add_candidate_capability(user_input)
        
        self.assertFalse(result["success"])
        self.assertIn("已存在", result["message"])
    
    def test_extract_capability_name(self):
        """测试提取能力名称"""
        # 测试"加一个X工具"格式
        user_input = "你给自己加一个星巴克菜单查询工具。"
        name = self.capability_router._extract_capability_name(user_input)
        
        self.assertIn("星巴克菜单查询", name)
    
    def test_extract_capability_description(self):
        """测试提取能力描述"""
        user_input = "你给自己加一个星巴克菜单查询工具。"
        description = self.capability_router._extract_capability_description(user_input)
        
        self.assertIn("星巴克菜单查询工具", description)
    
    def test_update_capability_status(self):
        """测试更新能力状态"""
        # 先添加一个能力
        user_input = "你给自己加一个星巴克菜单查询工具。"
        add_result = self.capability_router.add_candidate_capability(user_input)
        capability_name = add_result["capability_name"]
        
        # 更新状态为active
        result = self.capability_router.update_capability_status(capability_name, "active")
        
        self.assertTrue(result["success"])
        self.assertIn("已更新", result["message"])
        
        # 检查registry是否被更新
        registry_content = self.file_store.read_markdown("capabilities/registry.md")
        self.assertIn("active", registry_content)
    
    def test_update_nonexistent_capability(self):
        """测试更新不存在的能力状态"""
        result = self.capability_router.update_capability_status("nonexistent_capability", "active")
        
        self.assertFalse(result["success"])
        self.assertIn("不存在", result["message"])
    
    def test_get_capability_info(self):
        """测试获取能力信息"""
        # 先添加一个能力
        user_input = "你给自己加一个星巴克菜单查询工具。"
        add_result = self.capability_router.add_candidate_capability(user_input)
        capability_name = add_result["capability_name"]
        
        # 获取能力信息
        result = self.capability_router.get_capability_info(capability_name)
        
        self.assertTrue(result["success"])
        self.assertIn(capability_name, result["capability_name"])
        self.assertIn("candidate", result["info"])
    
    def test_get_nonexistent_capability_info(self):
        """测试获取不存在的能力信息"""
        result = self.capability_router.get_capability_info("nonexistent_capability")
        
        self.assertFalse(result["success"])
        self.assertIn("不存在", result["message"])
    
    def test_multiple_capability_addition(self):
        """测试添加多个能力"""
        # 添加第一个能力
        user_input1 = "你给自己加一个星巴克菜单查询工具。"
        result1 = self.capability_router.add_candidate_capability(user_input1)
        
        # 添加第二个能力
        user_input2 = "你加一个天气查询工具。"
        result2 = self.capability_router.add_candidate_capability(user_input2)
        
        self.assertTrue(result1["success"])
        self.assertTrue(result2["success"])
        
        # 检查registry是否包含两个能力
        registry_content = self.file_store.read_markdown("capabilities/registry.md")
        self.assertIn("星巴克菜单查询", registry_content)
        self.assertIn("天气查询", registry_content)


if __name__ == "__main__":
    unittest.main()