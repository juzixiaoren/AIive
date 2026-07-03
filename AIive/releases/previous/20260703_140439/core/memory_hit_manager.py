"""
Memory Hit Manager模块

实现记忆hit机制，记录记忆被读取的次数，用于评估记忆重要性。
"""

import json
from pathlib import Path
from datetime import datetime


class MemoryHitManager:
    """记忆命中管理器"""
    
    def __init__(self, project_root=None):
        """
        初始化记忆命中管理器
        
        Args:
            project_root: 项目根目录
        """
        if project_root:
            self.project_root = Path(project_root)
        else:
            self.project_root = Path(__file__).parent.parent
        
        self.index_dir = self.project_root / "memory" / "index"
        self.hits_file = self.index_dir / "memory_hits.json"
        
        # 确保目录存在
        self.index_dir.mkdir(parents=True, exist_ok=True)
        
        # 加载现有hit数据
        self.hits_data = self._load_hits()
    
    def _load_hits(self):
        """
        加载hit数据
        
        Returns:
            dict: hit数据
        """
        if self.hits_file.exists():
            try:
                with open(self.hits_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}
    
    def _save_hits(self):
        """
        保存hit数据
        """
        try:
            with open(self.hits_file, 'w', encoding='utf-8') as f:
                json.dump(self.hits_data, f, ensure_ascii=False, indent=2)
            return True
        except IOError:
            return False
    
    def record_hit(self, memory_path):
        """
        记录记忆命中
        
        Args:
            memory_path: 记忆文件路径
            
        Returns:
            dict: 记录结果
        """
        # 规范化路径
        memory_path = str(Path(memory_path)).replace("\\", "/")
        
        # 初始化或更新hit数据
        if memory_path not in self.hits_data:
            self.hits_data[memory_path] = {
                "hit_count": 0,
                "first_hit": datetime.now().isoformat(),
                "last_hit": None,
                "status": "active"
            }
        
        self.hits_data[memory_path]["hit_count"] += 1
        self.hits_data[memory_path]["last_hit"] = datetime.now().isoformat()
        
        # 保存
        if self._save_hits():
            return {
                "success": True,
                "memory_path": memory_path,
                "hit_count": self.hits_data[memory_path]["hit_count"]
            }
        else:
            return {
                "success": False,
                "message": "保存hit数据失败"
            }
    
    def get_hit_count(self, memory_path):
        """
        获取记忆命中次数
        
        Args:
            memory_path: 记忆文件路径
            
        Returns:
            int: 命中次数
        """
        memory_path = str(Path(memory_path)).replace("\\", "/")
        if memory_path in self.hits_data:
            return self.hits_data[memory_path]["hit_count"]
        return 0
    
    def get_hit_info(self, memory_path):
        """
        获取记忆命中信息
        
        Args:
            memory_path: 记忆文件路径
            
        Returns:
            dict: 命中信息
        """
        memory_path = str(Path(memory_path)).replace("\\", "/")
        if memory_path in self.hits_data:
            return {
                "success": True,
                "memory_path": memory_path,
                "info": self.hits_data[memory_path]
            }
        return {
            "success": False,
            "message": f"未找到记忆 {memory_path} 的命中记录"
        }
    
    def get_all_hits(self):
        """
        获取所有命中记录
        
        Returns:
            dict: 所有命中记录
        """
        return {
            "success": True,
            "hits": self.hits_data,
            "total_memories": len(self.hits_data)
        }
    
    def get_top_hits(self, limit=10):
        """
        获取命中次数最多的记忆
        
        Args:
            limit: 返回数量限制
            
        Returns:
            list: 排序后的记忆列表
        """
        sorted_memories = sorted(
            self.hits_data.items(),
            key=lambda x: x[1]["hit_count"],
            reverse=True
        )
        
        return {
            "success": True,
            "top_hits": [
                {
                    "path": path,
                    "hit_count": data["hit_count"],
                    "last_hit": data["last_hit"]
                }
                for path, data in sorted_memories[:limit]
            ]
        }
    
    def read_memory_with_hit(self, memory_path, file_store=None):
        """
        读取记忆并记录hit
        
        Args:
            memory_path: 记忆文件路径
            file_store: 文件存储器（用于读取文件）
            
        Returns:
            dict: 读取结果
        """
        # 记录hit
        hit_result = self.record_hit(memory_path)
        
        # 如果有file_store，读取文件内容
        content = None
        if file_store:
            try:
                content = file_store.read_markdown(memory_path)
            except Exception:
                pass
        
        return {
            "success": hit_result["success"],
            "memory_path": memory_path,
            "hit_count": hit_result.get("hit_count", 0),
            "content": content
        }


# 全局实例
_memory_hit_manager = None


def get_memory_hit_manager(project_root=None):
    """获取全局记忆命中管理器实例"""
    global _memory_hit_manager
    if _memory_hit_manager is None:
        _memory_hit_manager = MemoryHitManager(project_root)
    return _memory_hit_manager