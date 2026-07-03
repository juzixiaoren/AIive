"""
Health Check模块

检查系统健康状态。
"""

import os
from pathlib import Path


class HealthCheck:
    """健康检查器"""
    
    def __init__(self, project_root=None):
        """
        初始化健康检查器
        
        Args:
            project_root: 项目根目录，默认为当前文件所在目录的父目录
        """
        if project_root is None:
            self.project_root = Path(__file__).parent.parent
        else:
            self.project_root = Path(project_root)
    
    def run_check(self):
        """
        运行健康检查
        
        Returns:
            dict: 检查结果，包含pass和reasons
        """
        results = {
            "pass": True,
            "reasons": [],
            "checks": {}
        }
        
        # 检查必要目录
        results["checks"]["directories"] = self._check_directories()
        
        # 检查必要文件
        results["checks"]["files"] = self._check_files()
        
        # 检查mind文件
        results["checks"]["mind"] = self._check_mind_files()
        
        # 检查memory目录
        results["checks"]["memory"] = self._check_memory_dirs()
        
        # 检查capabilities
        results["checks"]["capabilities"] = self._check_capabilities()
        
        # 检查self_development
        results["checks"]["self_development"] = self._check_self_development()
        
        # 检查logs
        results["checks"]["logs"] = self._check_logs()
        
        # 汇总结果
        for check_name, check_result in results["checks"].items():
            if not check_result["pass"]:
                results["pass"] = False
                results["reasons"].extend(check_result["reasons"])
        
        return results
    
    def _check_directories(self):
        """
        检查必要目录
        
        Returns:
            dict: 检查结果
        """
        required_dirs = [
            "docs",
            "core",
            "mind",
            "memory",
            "capabilities",
            "self_development",
            "kernel",
            "releases",
            "logs",
            "tests"
        ]
        
        result = {
            "pass": True,
            "reasons": []
        }
        
        for dir_name in required_dirs:
            dir_path = self.project_root / dir_name
            if not dir_path.exists():
                result["pass"] = False
                result["reasons"].append(f"目录不存在: {dir_name}")
        
        return result
    
    def _check_files(self):
        """
        检查必要文件
        
        Returns:
            dict: 检查结果
        """
        required_files = [
            "main.py",
            "README.md"
        ]
        
        result = {
            "pass": True,
            "reasons": []
        }
        
        for file_name in required_files:
            file_path = self.project_root / file_name
            if not file_path.exists():
                result["pass"] = False
                result["reasons"].append(f"文件不存在: {file_name}")
        
        return result
    
    def _check_mind_files(self):
        """
        检查mind文件
        
        Returns:
            dict: 检查结果
        """
        required_files = [
            "system_prompt.md",
            "persona.md",
            "user_model.md",
            "listen_policy.md",
            "goals.md",
            "daily_context.md",
            "self_model.md"
        ]
        
        result = {
            "pass": True,
            "reasons": []
        }
        
        mind_dir = self.project_root / "mind"
        
        for file_name in required_files:
            file_path = mind_dir / file_name
            if not file_path.exists():
                result["pass"] = False
                result["reasons"].append(f"mind文件不存在: {file_name}")
        
        return result
    
    def _check_memory_dirs(self):
        """
        检查memory目录
        
        Returns:
            dict: 检查结果
        """
        required_dirs = [
            "preferences",
            "feedback",
            "events",
            "summaries",
            "archive",
            "index"
        ]
        
        result = {
            "pass": True,
            "reasons": []
        }
        
        memory_dir = self.project_root / "memory"
        
        for dir_name in required_dirs:
            dir_path = memory_dir / dir_name
            if not dir_path.exists():
                result["pass"] = False
                result["reasons"].append(f"memory目录不存在: {dir_name}")
        
        return result
    
    def _check_capabilities(self):
        """
        检查capabilities
        
        Returns:
            dict: 检查结果
        """
        result = {
            "pass": True,
            "reasons": []
        }
        
        # 检查registry.md
        registry_path = self.project_root / "capabilities" / "registry.md"
        if not registry_path.exists():
            result["pass"] = False
            result["reasons"].append("capabilities/registry.md不存在")
        
        return result
    
    def _check_self_development(self):
        """
        检查self_development
        
        Returns:
            dict: 检查结果
        """
        required_files = [
            "issues.md",
            "ideas.md",
            "changelog.md"
        ]
        
        result = {
            "pass": True,
            "reasons": []
        }
        
        self_dev_dir = self.project_root / "self_development"
        
        for file_name in required_files:
            file_path = self_dev_dir / file_name
            if not file_path.exists():
                result["pass"] = False
                result["reasons"].append(f"self_development文件不存在: {file_name}")
        
        return result
    
    def _check_logs(self):
        """
        检查logs
        
        Returns:
            dict: 检查结果
        """
        result = {
            "pass": True,
            "reasons": []
        }
        
        logs_dir = self.project_root / "logs"
        
        if not logs_dir.exists():
            result["pass"] = False
            result["reasons"].append("logs目录不存在")
        else:
            # 检查logs目录是否可写
            try:
                test_file = logs_dir / "test_write.tmp"
                test_file.write_text("test")
                test_file.unlink()
            except Exception as e:
                result["pass"] = False
                result["reasons"].append(f"logs目录不可写: {e}")
        
        return result