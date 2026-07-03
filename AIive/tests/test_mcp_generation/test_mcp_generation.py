"""
MCP生成能力测试

测试Agent是否能添加MCP（Model Context Protocol）能力
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


class TestMCPGeneration(unittest.TestCase):
    """MCP生成能力测试类"""
    
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
        self.capability_router = CapabilityRouter(self.file_store, str(self.project_root))
    
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
    
    def test_add_mcp(self):
        """测试添加MCP能力"""
        user_input = "给自己加一个数据库查询MCP"
        
        result = self.capability_router.add_mcp(user_input, "database_query")
        
        self.assertTrue(result["success"])
        self.assertEqual(result["capability_name"], "database_query")
        self.assertEqual(result["capability_type"], "mcp")
        
        # 验证目录已创建
        mcp_dir = self.project_root / "capabilities" / "mcp" / "database_query"
        self.assertTrue(mcp_dir.exists())
        
        # 验证README已创建
        readme_path = mcp_dir / "README.md"
        self.assertTrue(readme_path.exists())
        
        # 验证skeleton已创建
        skeleton_path = mcp_dir / "database_query.py"
        self.assertTrue(skeleton_path.exists())
        
        # 验证registry已更新
        registry_content = (self.project_root / "capabilities" / "registry.md").read_text(encoding='utf-8')
        self.assertIn("### database_query", registry_content)
        self.assertIn("Type: mcp", registry_content)
    
    def test_generate_skill_local_tool(self):
        """测试生成本地工具能力"""
        user_input = "给自己加一个天气查询工具"
        
        result = self.capability_router.generate_skill(user_input, "weather_query", "local_tool")
        
        self.assertTrue(result["success"])
        self.assertEqual(result["capability_type"], "local_tool")
        
        # 验证目录位置
        tool_dir = self.project_root / "capabilities" / "local_tools" / "weather_query"
        self.assertTrue(tool_dir.exists())
    
    def test_generate_skill_workflow(self):
        """测试生成工作流能力"""
        user_input = "给自己加一个日报生成工作流"
        
        result = self.capability_router.generate_skill(user_input, "daily_report", "workflow")
        
        self.assertTrue(result["success"])
        self.assertEqual(result["capability_type"], "workflow")
        
        # 验证目录位置
        workflow_dir = self.project_root / "capabilities" / "workflows" / "daily_report"
        self.assertTrue(workflow_dir.exists())
    
    def test_mcp_readme_content(self):
        """测试MCP README内容"""
        user_input = "给自己加一个API接入MCP"
        
        self.capability_router.add_mcp(user_input, "api_connector")
        
        readme_path = self.project_root / "capabilities" / "mcp" / "api_connector" / "README.md"
        content = readme_path.read_text(encoding='utf-8')
        
        self.assertIn("api_connector", content)
        self.assertIn("Skeleton", content)
    
    def test_mcp_skeleton_content(self):
        """测试MCP skeleton代码内容"""
        user_input = "给自己加一个文件系统MCP"
        
        self.capability_router.add_mcp(user_input, "filesystem")
        
        skeleton_path = self.project_root / "capabilities" / "mcp" / "filesystem" / "filesystem.py"
        content = skeleton_path.read_text(encoding='utf-8')
        
        self.assertIn("Filesystem", content)  # 类名
        self.assertIn("skeleton", content)
        self.assertIn("run", content)
    
    def test_mcp_registry_entry(self):
        """测试MCP注册表条目"""
        user_input = "给自己加一个消息队列MCP"
        
        self.capability_router.add_mcp(user_input, "message_queue")
        
        registry_content = (self.project_root / "capabilities" / "registry.md").read_text(encoding='utf-8')
        
        self.assertIn("### message_queue", registry_content)
        self.assertIn("Type: mcp", registry_content)
        self.assertIn("Status: candidate", registry_content)
        self.assertIn("capabilities/mcp/message_queue/message_queue.py", registry_content)
    
    def test_multiple_mcps(self):
        """测试添加多个MCP"""
        mcps = [
            ("数据库MCP", "database"),
            ("缓存MCP", "cache"),
            ("消息队列MCP", "message_queue")
        ]
        
        for user_input, name in mcps:
            result = self.capability_router.add_mcp(user_input, name)
            self.assertTrue(result["success"])
        
        # 验证所有MCP都已创建
        for _, name in mcps:
            mcp_dir = self.project_root / "capabilities" / "mcp" / name
            self.assertTrue(mcp_dir.exists())
            self.assertTrue((mcp_dir / "README.md").exists())
            self.assertTrue((mcp_dir / f"{name}.py").exists())
        
        # 验证registry包含所有MCP
        registry_content = (self.project_root / "capabilities" / "registry.md").read_text(encoding='utf-8')
        for _, name in mcps:
            self.assertIn(f"### {name}", registry_content)


if __name__ == '__main__':
    unittest.main()