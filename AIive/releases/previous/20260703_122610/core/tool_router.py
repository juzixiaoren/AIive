"""
Tool Router模块

路由工具调用。
"""


class ToolRouter:
    """工具路由器"""
    
    def __init__(self):
        """
        初始化工具路由器
        """
        # 注册的工具
        self.registered_tools = {}
    
    def register_tool(self, tool_name, tool_function, description=""):
        """
        注册工具
        
        Args:
            tool_name: 工具名称
            tool_function: 工具函数
            description: 工具描述
        """
        self.registered_tools[tool_name] = {
            "function": tool_function,
            "description": description
        }
    
    def call_tool(self, tool_name, *args, **kwargs):
        """
        调用工具
        
        Args:
            tool_name: 工具名称
            *args: 位置参数
            **kwargs: 关键字参数
            
        Returns:
            any: 工具返回值
        """
        if tool_name not in self.registered_tools:
            raise ValueError(f"工具 {tool_name} 未注册")
        
        tool = self.registered_tools[tool_name]
        return tool["function"](*args, **kwargs)
    
    def list_tools(self):
        """
        列出所有注册的工具
        
        Returns:
            list: 工具列表
        """
        return [
            {
                "name": name,
                "description": tool["description"]
            }
            for name, tool in self.registered_tools.items()
        ]
    
    def has_tool(self, tool_name):
        """
        检查工具是否存在
        
        Args:
            tool_name: 工具名称
            
        Returns:
            bool: 工具是否存在
        """
        return tool_name in self.registered_tools