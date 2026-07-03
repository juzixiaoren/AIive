"""
Memory Router模块

根据intent写入对应memory文件。
"""

import re
from datetime import datetime


class MemoryRouter:
    """记忆路由器"""
    
    def __init__(self, file_store):
        """
        初始化记忆路由器
        
        Args:
            file_store: 文件存储器
        """
        self.file_store = file_store
    
    def route_memory(self, user_input, intent_type):
        """
        根据意图路由记忆
        
        Args:
            user_input: 用户输入文本
            intent_type: 意图类型
            
        Returns:
            dict: 路由结果
        """
        if intent_type == "preference_update":
            return self._handle_preference_update(user_input)
        elif intent_type == "memory_update":
            return self._handle_memory_update(user_input)
        elif intent_type == "bug_report":
            return self._handle_bug_report(user_input)
        else:
            return {"success": False, "message": "不支持的记忆类型"}
    
    def _handle_preference_update(self, user_input):
        """
        处理偏好更新
        
        Args:
            user_input: 用户输入文本
            
        Returns:
            dict: 处理结果
        """
        # 判断是否是咖啡偏好
        coffee_keywords = ["咖啡", "瑞幸", "星巴克", "coffee", "luckin", "starbucks"]
        is_coffee = any(keyword in user_input.lower() for keyword in coffee_keywords)
        
        if is_coffee:
            file_path = "memory/preferences/coffee.md"
        else:
            file_path = "memory/preferences/general.md"
        
        # 生成记忆内容
        timestamp = self.file_store.get_timestamp()
        content = f"""
## {timestamp}

### Source
{user_input}

### Content
{self._extract_preference_content(user_input)}

### Type
preference

### Scope
{'咖啡推荐' if is_coffee else '通用偏好'}

### Confidence
high

### Notes
用户表达的长期偏好
"""
        
        # 写入文件
        success = self.file_store.append_markdown(file_path, content)
        
        if success:
            return {
                "success": True,
                "message": f"已记录到{'咖啡偏好' if is_coffee else '通用偏好'}中",
                "file_path": file_path
            }
        else:
            return {
                "success": False,
                "message": "写入偏好失败"
            }
    
    def _handle_memory_update(self, user_input):
        """
        处理记忆更新
        
        Args:
            user_input: 用户输入文本
            
        Returns:
            dict: 处理结果
        """
        file_path = "memory/events/user_requested_memory.md"
        
        # 生成记忆内容
        timestamp = self.file_store.get_timestamp()
        content = f"""
## {timestamp}

### Source
{user_input}

### Content
{self._extract_memory_content(user_input)}

### Type
event

### Scope
用户明确要求记住

### Confidence
high

### Notes
用户明确要求记住
"""
        
        # 写入文件
        success = self.file_store.append_markdown(file_path, content)
        
        if success:
            return {
                "success": True,
                "message": "已记录到事件记忆中",
                "file_path": file_path
            }
        else:
            return {
                "success": False,
                "message": "写入记忆失败"
            }
    
    def _handle_bug_report(self, user_input):
        """
        处理bug报告
        
        Args:
            user_input: 用户输入文本
            
        Returns:
            dict: 处理结果
        """
        file_path = "memory/feedback/interaction_errors.md"
        
        # 生成记忆内容
        timestamp = self.file_store.get_timestamp()
        content = f"""
## {timestamp}

### Source
{user_input}

### Content
{self._extract_bug_content(user_input)}

### Type
feedback

### Scope
交互错误

### Confidence
high

### Notes
用户报告的Agent行为问题
"""
        
        # 写入文件
        success = self.file_store.append_markdown(file_path, content)
        
        if success:
            return {
                "success": True,
                "message": "已记录到反馈记忆中",
                "file_path": file_path
            }
        else:
            return {
                "success": False,
                "message": "写入反馈失败"
            }
    
    def _extract_preference_content(self, user_input):
        """
        提取偏好内容
        
        Args:
            user_input: 用户输入文本
            
        Returns:
            str: 偏好内容
        """
        # 简单的提取逻辑
        if "爱" in user_input and "不爱" in user_input:
            # 处理"不爱X，爱Y"的格式
            parts = user_input.split("，")
            if len(parts) >= 2:
                return f"用户{parts[0].strip()}，{parts[1].strip()}"
        
        return f"用户表达偏好：{user_input}"
    
    def _extract_memory_content(self, user_input):
        """
        提取记忆内容
        
        Args:
            user_input: 用户输入文本
            
        Returns:
            str: 记忆内容
        """
        # 移除"记住"等关键词
        content = user_input
        for keyword in ["记住", "记录一下", "以后别忘了", "忘掉"]:
            content = content.replace(keyword, "")
        
        return content.strip() if content.strip() else user_input
    
    def _extract_bug_content(self, user_input):
        """
        提取bug内容
        
        Args:
            user_input: 用户输入文本
            
        Returns:
            str: bug内容
        """
        return f"用户报告问题：{user_input}"