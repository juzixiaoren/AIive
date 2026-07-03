"""
Test Runner模块

运行测试并捕获结果。
"""

import subprocess
import sys
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List


class TestRunner:
    """测试运行器"""
    
    def __init__(self, project_root: str = None):
        """
        初始化测试运行器
        
        Args:
            project_root: 项目根目录
        """
        if project_root:
            self.project_root = Path(project_root)
        else:
            self.project_root = Path(__file__).parent.parent
        
        self.logs_dir = self.project_root / ".jarvis_runtime" / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
    
    def run_pytest(self, test_path: str = "tests/", verbose: bool = True, timeout_seconds: int = 240) -> Dict[str, Any]:
        """
        运行pytest
        
        Args:
            test_path: 测试路径
            verbose: 是否详细输出
            timeout_seconds: 超时时间（秒），默认为240秒
            
        Returns:
            dict: 测试结果
        """
        cmd = ["python3", "-m", "pytest", test_path]
        if verbose:
            cmd.append("-v")
        
        return self._run_command(cmd, "pytest", timeout_seconds)
    
    def run_health_check(self, timeout_seconds: int = 240) -> Dict[str, Any]:
        """
        运行健康检查
        
        Args:
            timeout_seconds: 超时时间（秒），默认为240秒
            
        Returns:
            dict: 检查结果
        """
        cmd = ["python3", "main.py", "--health-check"]
        return self._run_command(cmd, "health_check", timeout_seconds)
    
    def run_specific_test(self, test_file: str, timeout_seconds: int = 240) -> Dict[str, Any]:
        """
        运行特定测试文件
        
        Args:
            test_file: 测试文件路径
            timeout_seconds: 超时时间（秒），默认为240秒
            
        Returns:
            dict: 测试结果
        """
        cmd = ["python3", "-m", "pytest", test_file, "-v"]
        return self._run_command(cmd, f"test_{Path(test_file).stem}", timeout_seconds)
    
    def _run_command(self, cmd: List[str], name: str, timeout_seconds: int = 240) -> Dict[str, Any]:
        """
        运行命令
        
        Args:
            cmd: 命令列表
            name: 日志名称
            timeout_seconds: 超时时间（秒），默认为240秒
            
        Returns:
            dict: 运行结果
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = self.logs_dir / f"{name}_{timestamp}.log"
        
        try:
            # 启动子进程，实时输出日志
            process = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1  # 行缓冲
            )
            
            stdout_lines = []
            stderr_lines = []
            
            # 实时读取并打印输出
            def read_stdout():
                while True:
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                    if line:
                        print(line, end='', flush=True)
                        stdout_lines.append(line)
            
            def read_stderr():
                while True:
                    line = process.stderr.readline()
                    if not line and process.poll() is not None:
                        break
                    if line:
                        print(line, end='', file=sys.stderr, flush=True)
                        stderr_lines.append(line)
            
            # 启动读取线程
            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            
            # 设置超时定时器
            timed_out = threading.Event()
            
            def timeout_handler():
                timed_out.set()
                process.terminate()
            
            timer = threading.Timer(timeout_seconds, timeout_handler)
            timer.start()
            
            try:
                # 等待进程结束
                returncode = process.wait()
            finally:
                timer.cancel()
                # 确保线程结束
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
            
            # 检查是否超时
            if timed_out.is_set():
                error_msg = f"{name} 超时（超过 {timeout_seconds} 秒）"
                self._log_error(log_file, error_msg)
                return {
                    "success": False,
                    "message": error_msg,
                    "log_file": str(log_file.relative_to(self.project_root))
                }
            
            success = returncode == 0
            stdout_text = ''.join(stdout_lines)
            stderr_text = ''.join(stderr_lines)
            
            # 写入日志
            log_content = f"""=== {name} 测试运行日志 ===
时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
命令: {' '.join(cmd)}
返回码: {returncode}
成功: {success}
超时时间: {timeout_seconds}秒

=== STDOUT ===
{stdout_text}

=== STDERR ===
{stderr_text}
"""
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(log_content)
            
            return {
                "success": success,
                "returncode": returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "log_file": str(log_file.relative_to(self.project_root)),
                "message": f"{name} {'通过' if success else '失败'}"
            }
        except Exception as e:
            error_msg = f"{name} 执行失败: {e}"
            self._log_error(log_file, error_msg)
            return {
                "success": False,
                "message": error_msg,
                "log_file": str(log_file.relative_to(self.project_root))
            }
    
    def _log_error(self, log_file: Path, error_msg: str):
        """记录错误日志"""
        try:
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(f"ERROR: {error_msg}\n")
        except Exception:
            pass
    
    def get_latest_log(self, name: str = None) -> str:
        """
        获取最新的日志内容
        
        Args:
            name: 日志名称筛选
            
        Returns:
            str: 日志内容
        """
        log_files = sorted(self.logs_dir.glob("*.log"), reverse=True)
        
        for log_file in log_files:
            if name and name not in log_file.name:
                continue
            
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception:
                continue
        
        return "无日志"
    
    def get_failure_logs(self) -> List[Dict[str, str]]:
        """
        获取失败的日志
        
        Returns:
            list: 失败日志列表
        """
        failures = []
        
        for log_file in self.logs_dir.glob("*.log"):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if "成功: False" in content or "ERROR" in content:
                        failures.append({
                            "file": log_file.name,
                            "content": content
                        })
            except Exception:
                continue
        
        return failures
    
    def clean_old_logs(self, keep_count: int = 10):
        """
        清理旧日志
        
        Args:
            keep_count: 保留数量
        """
        log_files = sorted(self.logs_dir.glob("*.log"), reverse=True)
        
        for log_file in log_files[keep_count:]:
            try:
                log_file.unlink()
            except Exception:
                pass


# 全局实例
_test_runner = None


def get_test_runner() -> TestRunner:
    """获取全局测试运行器实例"""
    global _test_runner
    if _test_runner is None:
        _test_runner = TestRunner()
    return _test_runner
