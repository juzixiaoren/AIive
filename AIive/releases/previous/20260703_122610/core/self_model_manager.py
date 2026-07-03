"""
Self Model Manager模块

更新mind/self_model.md。
"""

import re
from datetime import datetime


class SelfModelManager:
    """自我模型管理器"""
    
    def __init__(self, file_store):
        """
        初始化自我模型管理器
        
        Args:
            file_store: 文件存储器
        """
        self.file_store = file_store
    
    def update_self_model(self, event_type, description, files_modified=None):
        """
        更新自我模型
        
        Args:
            event_type: 事件类型
            description: 事件描述
            files_modified: 修改的文件列表
            
        Returns:
            dict: 更新结果
        """
        # 读取现有的self_model
        self_model_content = self.file_store.read_markdown("mind/self_model.md")
        
        # 生成更新内容
        timestamp = self.file_store.get_timestamp()
        update_entry = f"""

### {timestamp}

#### Event Type
{event_type}

#### Description
{description}

#### Files Modified
{', '.join(files_modified) if files_modified else '无'}

#### New Capabilities or Limitations
根据事件更新

#### Next Possible Actions
根据事件更新
"""
        
        # 追加到self_model
        success = self.file_store.append_markdown("mind/self_model.md", update_entry)
        
        if success:
            return {
                "success": True,
                "message": "已更新自我模型"
            }
        else:
            return {
                "success": False,
                "message": "更新自我模型失败"
            }
    
    def update_changelog(self, change_type, description, files_modified=None, result="成功", next_actions=None):
        """
        更新changelog
        
        Args:
            change_type: 变更类型
            description: 变更描述
            files_modified: 修改的文件列表
            result: 结果
            next_actions: 后续动作
            
        Returns:
            dict: 更新结果
        """
        # 生成changelog内容
        timestamp = self.file_store.get_timestamp()
        changelog_entry = f"""

## {timestamp}

### Change
{change_type}

### Reason
{description}

### Files
{', '.join(files_modified) if files_modified else '无'}

### Result
{result}

### Next
{next_actions if next_actions else '无'}
"""
        
        # 追加到changelog
        success = self.file_store.append_markdown("self_development/changelog.md", changelog_entry)
        
        if success:
            return {
                "success": True,
                "message": "已更新changelog"
            }
        else:
            return {
                "success": False,
                "message": "更新changelog失败"
            }
    
    def create_issue(self, title, source, user_request, issue_type, related_files=None, proposed_plan=None, priority="medium"):
        """
        创建issue
        
        Args:
            title: issue标题
            source: 来源
            user_request: 用户请求
            issue_type: issue类型
            related_files: 相关文件
            proposed_plan: 建议计划
            priority: 优先级
            
        Returns:
            dict: 创建结果
        """
        # 生成issue内容
        timestamp = self.file_store.get_timestamp()
        issue_entry = f"""

## [Open] {title}

- Created At: {timestamp}
- Source: {source}
- User Request: {user_request}
- Type: {issue_type}
- Related Files: {', '.join(related_files) if related_files else '无'}
- Proposed Plan: {proposed_plan if proposed_plan else '待规划'}
- Priority: {priority}
- Status: Open
- Notes: 用户请求的自我开发任务
"""
        
        # 追加到issues
        success = self.file_store.append_markdown("self_development/issues.md", issue_entry)
        
        if success:
            return {
                "success": True,
                "message": f"已创建待开发任务：{title}",
                "issue_title": title
            }
        else:
            return {
                "success": False,
                "message": "创建任务失败"
            }