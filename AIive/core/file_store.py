"""
File Store模块

提供基础文件读写能力。
"""

import os
from pathlib import Path
from datetime import datetime


class FileStore:
    """文件存储器"""
    
    def __init__(self, project_root=None):
        """
        初始化文件存储器
        
        Args:
            project_root: 项目根目录，默认为当前文件所在目录的父目录
        """
        if project_root is None:
            self.project_root = Path(__file__).parent.parent
        else:
            self.project_root = Path(project_root)
    
    def read_markdown(self, file_path):
        """
        读取Markdown文件
        
        Args:
            file_path: 文件路径（相对于项目根目录）
            
        Returns:
            str: 文件内容
        """
        full_path = self.project_root / file_path
        
        if not full_path.exists():
            return ""
        
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            print(f"读取文件失败: {e}")
            return ""
    
    def write_markdown(self, file_path, content, append=False):
        """
        写入Markdown文件
        
        Args:
            file_path: 文件路径（相对于项目根目录）
            content: 文件内容
            append: 是否追加模式
            
        Returns:
            bool: 是否成功写入
        """
        full_path = self.project_root / file_path
        
        # 确保目录存在
        full_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            mode = 'a' if append else 'w'
            with open(full_path, mode, encoding='utf-8') as f:
                f.write(content)
            return True
        except Exception as e:
            print(f"写入文件失败: {e}")
            return False
    
    def append_markdown(self, file_path, content):
        """
        追加Markdown内容
        
        Args:
            file_path: 文件路径（相对于项目根目录）
            content: 追加的内容
            
        Returns:
            bool: 是否成功追加
        """
        return self.write_markdown(file_path, content, append=True)
    
    def ensure_directory(self, dir_path):
        """
        确保目录存在
        
        Args:
            dir_path: 目录路径（相对于项目根目录）
            
        Returns:
            bool: 是否成功创建
        """
        full_path = self.project_root / dir_path
        
        try:
            full_path.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            print(f"创建目录失败: {e}")
            return False
    
    def write_log(self, log_file, message, level="INFO"):
        """
        写入日志
        
        Args:
            log_file: 日志文件路径（相对于项目根目录）
            message: 日志消息
            level: 日志级别
            
        Returns:
            bool: 是否成功写入
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {message}\n"
        
        return self.append_markdown(log_file, log_entry)
    
    def get_timestamp(self):
        """
        获取当前时间戳
        
        Returns:
            str: 时间戳字符串
        """
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    def file_exists(self, file_path):
        """
        检查文件是否存在
        
        Args:
            file_path: 文件路径（相对于项目根目录）
            
        Returns:
            bool: 文件是否存在
        """
        full_path = self.project_root / file_path
        return full_path.exists()
    
    def list_files(self, dir_path, pattern="*"):
        """
        列出目录下的文件
        
        Args:
            dir_path: 目录路径（相对于项目根目录）
            pattern: 文件模式
            
        Returns:
            list: 文件路径列表
        """
        full_path = self.project_root / dir_path
        
        if not full_path.exists():
            return []
        
        try:
            return [str(f.relative_to(self.project_root)) for f in full_path.glob(pattern) if f.is_file()]
        except Exception as e:
            print(f"列出文件失败: {e}")
            return []