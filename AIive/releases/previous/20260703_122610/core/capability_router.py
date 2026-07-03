"""
Capability Router模块

维护capabilities/registry.md，支持完整的能力生成（目录+骨架+注册表）。
"""

import re
import os
from pathlib import Path
from datetime import datetime


class CapabilityRouter:
    """能力路由器"""
    
    def __init__(self, file_store, project_root=None):
        """
        初始化能力路由器
        
        Args:
            file_store: 文件存储器
            project_root: 项目根目录
        """
        self.file_store = file_store
        if project_root:
            self.project_root = Path(project_root)
        else:
            self.project_root = Path(file_store.root_path) if hasattr(file_store, 'root_path') else Path(".")
    
    def add_candidate_capability(self, user_input, capability_name=None):
        """
        添加候选能力
        
        Args:
            user_input: 用户输入文本
            capability_name: 能力名称，如果为None则从输入中提取
            
        Returns:
            dict: 添加结果
        """
        if capability_name is None:
            capability_name = self._extract_capability_name(user_input)
        
        # 读取现有的registry
        registry_content = self.file_store.read_markdown("capabilities/registry.md")
        
        # 检查能力是否已存在
        if f"### {capability_name}" in registry_content:
            return {
                "success": False,
                "message": f"能力 {capability_name} 已存在"
            }
        
        # 生成新的能力条目
        timestamp = self.file_store.get_timestamp()
        capability_entry = f"""

### {capability_name}

- Status: candidate
- Type: local_tool
- Description: {self._extract_capability_description(user_input)}
- Created At: {timestamp}
- Last Used: 
- Hit Count: 0
- Wake Conditions: 用户要求使用时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: 
- Notes: 用户请求的候选能力，尚未实现
"""
        
        # 追加到registry
        success = self.file_store.append_markdown("capabilities/registry.md", capability_entry)
        
        if success:
            return {
                "success": True,
                "message": f"已注册为候选能力：{capability_name}。当前尚未实现真实查询工具，我已把它加入自我开发任务。",
                "capability_name": capability_name
            }
        else:
            return {
                "success": False,
                "message": "注册能力失败"
            }
    
    def _extract_capability_name(self, user_input):
        """
        提取能力名称
        
        Args:
            user_input: 用户输入文本
            
        Returns:
            str: 能力名称
        """
        # 尝试提取工具名称
        patterns = [
            r"加一个(.+?)工具",
            r"加一个(.+?)查询",
            r"给自己加一个(.+?)",
            r"写个工具(.+?)",
            r"加一个(.+?)MCP"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, user_input)
            if match:
                name = match.group(1).strip()
                # 清理名称，只保留字母、数字、下划线
                name = re.sub(r'[^\w\s]', '', name)
                name = name.replace(' ', '_')
                return name
        
        # 默认名称
        return "new_capability"
    
    def _extract_capability_description(self, user_input):
        """
        提取能力描述
        
        Args:
            user_input: 用户输入文本
            
        Returns:
            str: 能力描述
        """
        # 简单的描述提取
        return f"用户请求的能力：{user_input}"
    
    def update_capability_status(self, capability_name, new_status):
        """
        更新能力状态
        
        Args:
            capability_name: 能力名称
            new_status: 新状态
            
        Returns:
            dict: 更新结果
        """
        # 读取现有的registry
        registry_content = self.file_store.read_markdown("capabilities/registry.md")
        
        # 检查能力是否存在
        if f"### {capability_name}" not in registry_content:
            return {
                "success": False,
                "message": f"能力 {capability_name} 不存在"
            }
        
        # 更新状态
        pattern = f"(### {capability_name}.*?\\- Status: )\\w+"
        replacement = f"\\g<1>{new_status}"
        
        new_content = re.sub(pattern, replacement, registry_content, flags=re.DOTALL)
        
        # 写入文件
        success = self.file_store.write_markdown("capabilities/registry.md", new_content)
        
        if success:
            return {
                "success": True,
                "message": f"已更新能力 {capability_name} 状态为 {new_status}"
            }
        else:
            return {
                "success": False,
                "message": "更新能力状态失败"
            }
    
    def get_capability_info(self, capability_name):
        """
        获取能力信息
        
        Args:
            capability_name: 能力名称
            
        Returns:
            dict: 能力信息
        """
        # 读取现有的registry
        registry_content = self.file_store.read_markdown("capabilities/registry.md")
        
        # 检查能力是否存在
        if f"### {capability_name}" not in registry_content:
            return {
                "success": False,
                "message": f"能力 {capability_name} 不存在"
            }
        
        # 提取能力信息
        pattern = f"### {capability_name}(.*?)(?=###|$)"
        match = re.search(pattern, registry_content, re.DOTALL)
        
        if match:
            info_text = match.group(1).strip()
            return {
                "success": True,
                "capability_name": capability_name,
                "info": info_text
            }
        else:
            return {
                "success": False,
                "message": "无法提取能力信息"
            }
    
    def generate_skill(self, user_input, capability_name=None, capability_type="local_tool"):
        """
        生成完整的能力骨架（目录+README+skeleton+注册表）
        
        Args:
            user_input: 用户输入文本
            capability_name: 能力名称
            capability_type: 能力类型 (local_tool / mcp / workflow / ui_component)
            
        Returns:
            dict: 生成结果
        """
        if capability_name is None:
            capability_name = self._extract_capability_name(user_input)
        
        # 根据类型确定目录
        type_dir_map = {
            "local_tool": "capabilities/local_tools",
            "mcp": "capabilities/mcp",
            "workflow": "capabilities/workflows",
            "ui_component": "capabilities/ui_components"
        }
        
        base_dir = type_dir_map.get(capability_type, "capabilities/local_tools")
        capability_dir = self.project_root / base_dir / capability_name
        
        try:
            # 1. 创建能力目录
            capability_dir.mkdir(parents=True, exist_ok=True)
            
            # 2. 生成README
            description = self._extract_capability_description(user_input)
            readme_content = self._generate_readme(capability_name, description, capability_type)
            readme_path = capability_dir / "README.md"
            readme_path.write_text(readme_content, encoding='utf-8')
            
            # 3. 生成skeleton工具文件
            skeleton_content = self._generate_skeleton(capability_name, capability_type)
            skeleton_path = capability_dir / f"{capability_name}.py"
            skeleton_path.write_text(skeleton_content, encoding='utf-8')
            
            # 4. 更新registry
            timestamp = self.file_store.get_timestamp()
            implementation_path = f"{base_dir}/{capability_name}/{capability_name}.py"
            registry_entry = f"""

### {capability_name}

- Status: candidate
- Type: {capability_type}
- Description: {description}
- Created At: {timestamp}
- Last Used: 
- Hit Count: 0
- Wake Conditions: 用户要求使用时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: {implementation_path}
- Notes: Skeleton实现，待完善
"""
            
            registry_success = self.file_store.append_markdown("capabilities/registry.md", registry_entry)
            
            if registry_success:
                return {
                    "success": True,
                    "message": f"已生成能力骨架：{capability_name}",
                    "capability_name": capability_name,
                    "capability_type": capability_type,
                    "directory": str(capability_dir.relative_to(self.project_root)),
                    "files_created": [
                        str(readme_path.relative_to(self.project_root)),
                        str(skeleton_path.relative_to(self.project_root))
                    ]
                }
            else:
                return {
                    "success": False,
                    "message": f"目录和文件已创建，但注册表更新失败"
                }
                
        except Exception as e:
            return {
                "success": False,
                "message": f"生成能力骨架失败: {str(e)}"
            }
    
    def add_mcp(self, user_input, mcp_name=None):
        """
        添加MCP（Model Context Protocol）能力
        
        Args:
            user_input: 用户输入文本
            mcp_name: MCP名称
            
        Returns:
            dict: 添加结果
        """
        return self.generate_skill(user_input, mcp_name, capability_type="mcp")
    
    def _generate_readme(self, capability_name, description, capability_type):
        """
        生成README内容
        
        Args:
            capability_name: 能力名称
            description: 能力描述
            capability_type: 能力类型
            
        Returns:
            str: README内容
        """
        return f"""# {capability_name}

## 简介
{description}

## 类型
{capability_type}

## 功能
- [待实现] 核心功能1
- [待实现] 核心功能2

## 使用方式
```python
# 示例代码
from {capability_name} import {capability_name}

# 初始化
tool = {capability_name}()

# 使用
result = tool.run()
```

## 状态
Skeleton - 尚未实现真实功能

## 文件结构
```
{capability_name}/
├── README.md          # 本文档
└── {capability_name}.py  # 主要实现
```
"""
    
    def _generate_skeleton(self, capability_name, capability_type):
        """
        生成skeleton代码
        
        Args:
            capability_name: 能力名称
            capability_type: 能力类型
            
        Returns:
            str: skeleton代码
        """
        class_name = ''.join(word.capitalize() for word in capability_name.split('_'))
        
        return f'''"""
{capability_name} - {capability_type}能力

Skeleton实现，待完善。
"""


class {class_name}:
    """{capability_name}能力类"""
    
    def __init__(self):
        """初始化"""
        self.name = "{capability_name}"
        self.type = "{capability_type}"
        self.status = "skeleton"
    
    def run(self, *args, **kwargs):
        """
        运行能力
        
        Returns:
            dict: 运行结果
        """
        return {{
            "success": False,
            "message": f"{{self.name}} 尚未实现",
            "status": "skeleton"
        }}
    
    def is_available(self):
        """检查能力是否可用"""
        return False
    
    def get_info(self):
        """获取能力信息"""
        return {{
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "description": "Skeleton实现，待完善"
        }}


def main():
    """测试入口"""
    tool = {class_name}()
    print(tool.get_info())
    result = tool.run()
    print(result)


if __name__ == "__main__":
    main()
'''