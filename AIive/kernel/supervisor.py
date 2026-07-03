"""
Supervisor模块

管理Agent的启动和状态。
"""

import os
import sys
from pathlib import Path


class Supervisor:
    """Agent监督器"""
    
    def __init__(self, project_root=None):
        """
        初始化监督器
        
        Args:
            project_root: 项目根目录，默认为当前文件所在目录的父目录
        """
        if project_root is None:
            self.project_root = Path(__file__).parent.parent
        else:
            self.project_root = Path(project_root)
    
    def run_health_check(self):
        """
        运行健康检查
        
        Returns:
            dict: 健康检查结果
        """
        from kernel.health_check import HealthCheck
        
        health_check = HealthCheck(self.project_root)
        return health_check.run_check()
    
    def show_status(self):
        """
        显示Agent状态
        
        Returns:
            dict: Agent状态信息
        """
        status = {
            "project_root": str(self.project_root),
            "health_check": self.run_health_check(),
            "version_info": self._get_version_info(),
        }
        
        return status
    
    def _get_version_info(self):
        """
        获取版本信息
        
        Returns:
            dict: 版本信息
        """
        releases_dir = self.project_root / "releases"
        
        version_info = {
            "current": None,
            "candidate": None,
            "previous": None,
        }
        
        # 检查current版本
        current_dir = releases_dir / "current"
        if current_dir.exists() and any(current_dir.iterdir()):
            version_info["current"] = "exists"
        
        # 检查candidate版本
        candidate_dir = releases_dir / "candidate"
        if candidate_dir.exists() and any(candidate_dir.iterdir()):
            version_info["candidate"] = "exists"
        
        # 检查previous版本
        previous_dir = releases_dir / "previous"
        if previous_dir.exists() and any(previous_dir.iterdir()):
            version_info["previous"] = "exists"
        
        return version_info


def main():
    """CLI入口"""
    supervisor = Supervisor()
    status = supervisor.show_status()
    
    print("Agent状态:")
    print(f"项目根目录: {status['project_root']}")
    print(f"健康检查: {'通过' if status['health_check']['pass'] else '失败'}")
    
    if not status['health_check']['pass']:
        print("失败原因:")
        for reason in status['health_check']['reasons']:
            print(f"  - {reason}")
    
    print(f"版本信息:")
    print(f"  当前版本: {status['version_info']['current'] or '无'}")
    print(f"  候选版本: {status['version_info']['candidate'] or '无'}")
    print(f"  历史版本: {status['version_info']['previous'] or '无'}")


if __name__ == "__main__":
    main()