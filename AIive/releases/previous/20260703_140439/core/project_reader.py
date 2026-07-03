"""
Project Reader模块

提供项目文件读取能力，禁止删除操作。
"""

import os
from pathlib import Path

from core.utils import timeout


class ProjectReader:
    """项目读取器"""
    
    # 默认排除的目录和文件
    DEFAULT_EXCLUDES = [
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".idea",
        ".vscode",
        "*.pyc",
        ".env"
    ]
    
    def __init__(self, project_root: str | None = None):
        """
        初始化项目读取器
        
        Args:
            project_root: 项目根目录，默认为当前文件所在目录的父目录
        """
        if project_root:
            self.project_root = Path(project_root)
        else:
            self.project_root = Path(__file__).parent.parent
    
    @timeout(10)
    def list_files(
        self,
        root: str = ".",
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None
    ) -> list[str]:
        """
        列出文件
        
        Args:
            root: 相对根目录
            include_patterns: 包含模式列表
            exclude_patterns: 排除模式列表
            
        Returns:
            list: 文件路径列表（相对路径）
        """
        target_dir = self.project_root / root
        if not target_dir.exists():
            return []
        
        excludes = self.DEFAULT_EXCLUDES + (exclude_patterns or [])
        
        files = []
        for item in target_dir.rglob("*"):
            if item.is_file():
                rel_path = str(item.relative_to(self.project_root))
                
                # 检查是否在排除列表中
                if self._is_excluded(rel_path, excludes):
                    continue
                
                # 检查是否匹配包含模式
                if include_patterns and not self._matches_patterns(rel_path, include_patterns):
                    continue
                
                files.append(rel_path)
        
        return sorted(files)
    
    @timeout(10)
    def read_file(self, path: str) -> str:
        """
        读取文件内容
        
        Args:
            path: 相对路径
            
        Returns:
            str: 文件内容
        """
        full_path = self.project_root / path
        
        if not full_path.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            raise RuntimeError(f"读取文件失败 {path}: {e}")
    
    @timeout(10)
    def read_files(self, paths: list[str]) -> dict[str, str]:
        """
        批量读取文件
        
        Args:
            paths: 文件路径列表
            
        Returns:
            dict: {路径: 内容}
        """
        result = {}
        for path in paths:
            try:
                result[path] = self.read_file(path)
            except (FileNotFoundError, RuntimeError) as e:
                result[path] = f"[ERROR: {e}]"
        return result
    
    @timeout(10)
    def summarize_project_tree(self, max_depth: int = 3) -> str:
        """
        生成项目树摘要
        
        Args:
            max_depth: 最大深度
            
        Returns:
            str: 项目树字符串
        """
        lines = []
        self._build_tree(self.project_root, lines, "", max_depth, 0)
        return "\n".join(lines)
    
    def _build_tree(
        self,
        path: Path,
        lines: list[str],
        prefix: str,
        max_depth: int,
        current_depth: int
    ):
        """递归构建目录树"""
        if current_depth > max_depth:
            return
        
        # 获取当前目录下的所有条目
        try:
            entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name))
        except PermissionError:
            return
        
        for i, entry in enumerate(entries):
            # 检查是否排除
            rel_path = str(entry.relative_to(self.project_root))
            if self._is_excluded(rel_path, self.DEFAULT_EXCLUDES):
                continue
            
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            
            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                extension = "    " if is_last else "│   "
                self._build_tree(entry, lines, prefix + extension, max_depth, current_depth + 1)
            else:
                lines.append(f"{prefix}{connector}{entry.name}")
    
    @timeout(10)
    def search_text(self, query: str, root: str = ".") -> list[dict[str, object]]:
        """
        搜索文本
        
        Args:
            query: 搜索关键词
            root: 搜索根目录
            
        Returns:
            list: 匹配结果列表 [{file, line, content}]
        """
        target_dir = self.project_root / root
        if not target_dir.exists():
            return []
        
        results = []
        
        for file_path in target_dir.rglob("*"):
            if not file_path.is_file():
                continue
            
            rel_path = str(file_path.relative_to(self.project_root))
            
            # 只搜索文本文件
            if not self._is_text_file(file_path):
                continue
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        if query.lower() in line.lower():
                            results.append({
                                "file": rel_path,
                                "line": line_num,
                                "content": line.strip()
                            })
            except (UnicodeDecodeError, PermissionError):
                continue
        
        return results
    
    def _is_excluded(self, path: str, excludes: list[str]) -> bool:
        """检查路径是否在排除列表中"""
        for exclude in excludes:
            if exclude.startswith("*"):
                # 通配符模式
                if path.endswith(exclude[1:]):
                    return True
            elif exclude in path.split(os.sep):
                return True
        return False
    
    def _matches_patterns(self, path: str, patterns: list[str]) -> bool:
        """检查路径是否匹配模式"""
        import fnmatch
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
        return False
    
    def _is_text_file(self, path: Path) -> bool:
        """检查是否是文本文件"""
        text_extensions = {
            '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css',
            '.json', '.md', '.txt', '.yml', '.yaml', '.toml',
            '.cfg', '.ini', '.sh', '.bash', '.env.example'
        }
        return path.suffix.lower() in text_extensions
    
    @timeout(10)
    def file_exists(self, path: str) -> bool:
        """
        检查文件是否存在
        
        Args:
            path: 相对路径
            
        Returns:
            bool: 是否存在
        """
        return (self.project_root / path).exists()
    
    def get_project_root(self) -> str:
        """
        获取项目根目录
        
        Returns:
            str: 项目根目录路径
        """
        return str(self.project_root)


# 全局实例
_project_reader: ProjectReader | None = None


def get_project_reader() -> ProjectReader:
    """获取全局项目读取器实例"""
    global _project_reader
    if _project_reader is None:
        _project_reader = ProjectReader()
    return _project_reader
