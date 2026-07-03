"""
Self Repair Loop模块

当candidate测试失败时，自动尝试修复。
"""

import json
from typing import Dict, Any, List
from core.llm_client import LLMClient
from core.decision_engine import DecisionEngine
from core.update_executor import UpdateExecutor


class SelfRepairLoop:
    """自修复循环"""
    
    def __init__(
        self,
        llm_client: LLMClient,
        decision_engine: DecisionEngine,
        update_executor: UpdateExecutor | None = None,
        max_repair_attempts: int = 2
    ):
        """
        初始化自修复循环
        
        Args:
            llm_client: LLM客户端
            decision_engine: 决策引擎
            update_executor: 更新执行器
            max_repair_attempts: 最大修复尝试次数
        """
        self.llm_client = llm_client
        self.decision_engine = decision_engine
        self.update_executor = update_executor or UpdateExecutor()
        self.max_repair_attempts = max_repair_attempts
    
    def attempt_repair(
        self,
        original_request: str,
        failed_operations: List[Dict[str, Any]],
        error_logs: str,
        changed_files: Dict[str, str],
        failure_analysis: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """
        尝试修复失败的操作
        
        Args:
            original_request: 原始用户请求
            failed_operations: 失败的操作
            error_logs: 错误日志
            changed_files: 已修改的文件
            failure_analysis: 失败分析结果
            
        Returns:
            dict: 修复结果
        """
        repair_history = []
        
        for attempt in range(self.max_repair_attempts):
            # 构建修复上下文
            repair_context = self._build_repair_context(
                original_request,
                failed_operations,
                error_logs,
                changed_files,
                repair_history
            )
            
            # 调用LLM生成修复方案
            repair_decision = self._ask_llm_for_repair(repair_context, failure_analysis)
            
            if not repair_decision.get("success"):
                repair_history.append({
                    "attempt": attempt + 1,
                    "success": False,
                    "error": "LLM无法生成修复方案"
                })
                continue
            
            # 返回修复方案
            return {
                "success": True,
                "attempt": attempt + 1,
                "repair_operations": repair_decision.get("operations", []),
                "repair_plan": repair_decision.get("plan", []),
                "message": f"生成修复方案（第{attempt + 1}次尝试）"
            }
        
        return {
            "success": False,
            "attempts": self.max_repair_attempts,
            "repair_history": repair_history,
            "message": f"经过{self.max_repair_attempts}次尝试仍无法修复"
        }
    
    def _build_repair_context(
        self,
        original_request: str,
        failed_operations: List[Dict[str, Any]],
        error_logs: str,
        changed_files: Dict[str, str],
        repair_history: List[Dict[str, Any]]
    ) -> str:
        """
        构建修复上下文
        
        Args:
            original_request: 原始请求
            failed_operations: 失败的操作
            error_logs: 错误日志
            changed_files: 已修改的文件
            repair_history: 修复历史
            
        Returns:
            str: 修复上下文
        """
        parts = []
        
        parts.append("## 原始用户请求\n")
        parts.append(original_request)
        parts.append("\n\n")
        
        parts.append("## 失败的操作\n")
        parts.append(json.dumps(failed_operations, indent=2, ensure_ascii=False))
        parts.append("\n\n")
        
        parts.append("## 错误日志\n")
        parts.append("```\n")
        parts.append(error_logs[:3000])  # 限制长度
        parts.append("\n```\n\n")
        
        parts.append("## 已修改的文件内容\n")
        for file_path, content in changed_files.items():
            parts.append(f"### {file_path}\n")
            parts.append(content[:1000])  # 限制长度
            if len(content) > 1000:
                parts.append("\n... (截断)")
            parts.append("\n\n")
        
        if repair_history:
            parts.append("## 之前的修复尝试\n")
            for history in repair_history:
                parts.append(f"- 第{history['attempt']}次尝试: {'成功' if history['success'] else '失败'}")
                if 'error' in history:
                    parts.append(f" - {history['error']}")
            parts.append("\n\n")
        
        parts.append("## 要求\n")
        parts.append("请分析错误原因，生成修复方案。输出格式与之前相同的JSON决策格式。")
        
        return "".join(parts)
    
    def _ask_llm_for_repair(self, context: str, failure_analysis: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """
        请求LLM生成修复方案
        
        Args:
            context: 修复上下文
            failure_analysis: 失败分析结果
            
        Returns:
            dict: LLM响应
        """
        # 根据失败分析构建更精确的提示
        analysis_hint = ""
        if failure_analysis:
            failure_type = failure_analysis.get("failure_type", "unknown")
            if failure_type == "assertion_error":
                analysis_hint = """
测试失败原因是断言错误（assertion error），可能是：
1. 测试脚本的期望值与实际行为不匹配
2. 代码逻辑确实有问题

请分析具体原因：
- 如果是测试脚本的问题，可以修改测试脚本来匹配正确的代码行为
- 如果是代码问题，修改代码
"""
            elif failure_type == "import_error":
                analysis_hint = """
测试失败原因是导入错误（import error），可能是：
1. 缺少依赖
2. 模块路径错误
3. 循环导入

请分析具体原因并修复。
"""
            elif failure_type == "syntax_error":
                analysis_hint = """
测试失败原因是语法错误（syntax error），请修复语法问题。
"""
            elif failure_type == "test_script_error":
                analysis_hint = """
测试失败原因是测试脚本本身的问题（如LLM回复内容断言），不是代码bug。
可以：
1. 修改测试脚本使其更灵活
2. 跳过该测试
3. 如果是预期行为变化，更新测试期望值
"""
        
        messages = [
            {
                "role": "system",
                "content": f"""你是一个代码修复专家。用户之前的请求执行失败了，请分析错误日志，生成修复方案。

你必须输出与之前相同的JSON决策格式，包含：
- request_type
- summary
- operations（修复操作列表）
- plan（修复计划）

注意：
- 只修复导致失败的具体问题
- 不要重复之前已经做过的事情
- 确保修复后的代码能通过测试
- 禁止删除操作
- 操作类型支持：read_file | write_file | append_file | patch_file | create_file | create_issue | update_registry | run_tests
{analysis_hint}"""
            },
            {
                "role": "user",
                "content": context
            }
        ]
        
        try:
            decision = self.llm_client.chat_json(messages, temperature=0.3)
            
            # 验证格式
            if "operations" in decision:
                return {
                    "success": True,
                    **decision
                }
            else:
                return {"success": False}
                
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _analyze_test_failures(self, test_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析测试失败原因，区分代码bug和测试脚本问题
        
        Args:
            test_result: 测试结果
            
        Returns:
            dict: 分析结果
        """
        stderr = test_result.get("stderr", "")
        stdout = test_result.get("stdout", "")
        combined = stderr + "\n" + stdout
        
        analysis = {
            "failure_type": "unknown",
            "is_code_bug": True,
            "recommendation": "modify_code",
            "details": []
        }
        
        # 检查断言错误
        if "AssertionError" in combined or "assert" in combined.lower():
            analysis["failure_type"] = "assertion_error"
            analysis["is_code_bug"] = True  # 默认为代码问题，但需要进一步分析
            analysis["recommendation"] = "analyze_and_fix"
            analysis["details"].append("断言失败，需要分析是代码问题还是测试期望问题")
        
        # 检查导入错误
        elif "ImportError" in combined or "ModuleNotFoundError" in combined:
            analysis["failure_type"] = "import_error"
            analysis["is_code_bug"] = True
            analysis["recommendation"] = "fix_imports"
            analysis["details"].append("导入错误，可能是依赖问题或路径问题")
        
        # 检查语法错误
        elif "SyntaxError" in combined:
            analysis["failure_type"] = "syntax_error"
            analysis["is_code_bug"] = True
            analysis["recommendation"] = "fix_syntax"
            analysis["details"].append("语法错误，需要修复代码语法")
        
        # 检查类型错误
        elif "TypeError" in combined:
            analysis["failure_type"] = "type_error"
            analysis["is_code_bug"] = True
            analysis["recommendation"] = "fix_type"
            analysis["details"].append("类型错误，需要修复类型相关问题")
        
        # 检查属性错误
        elif "AttributeError" in combined:
            analysis["failure_type"] = "attribute_error"
            analysis["is_code_bug"] = True
            analysis["recommendation"] = "fix_attribute"
            analysis["details"].append("属性错误，可能是对象缺少属性或方法")
        
        # 检查索引错误
        elif "IndexError" in combined:
            analysis["failure_type"] = "index_error"
            analysis["is_code_bug"] = True
            analysis["recommendation"] = "fix_index"
            analysis["details"].append("索引错误，可能是数组越界")
        
        # 检查键错误
        elif "KeyError" in combined:
            analysis["failure_type"] = "key_error"
            analysis["is_code_bug"] = True
            analysis["recommendation"] = "fix_key"
            analysis["details"].append("键错误，可能是字典缺少键")
        
        # 检查超时
        elif "TimeoutError" in combined or "timeout" in combined.lower():
            analysis["failure_type"] = "timeout"
            analysis["is_code_bug"] = False
            analysis["recommendation"] = "optimize_or_increase_timeout"
            analysis["details"].append("超时，可能是性能问题或测试时间不足")
        
        # 检查LLM回复内容断言（测试脚本问题）
        elif "LLM" in combined and ("reply" in combined.lower() or "response" in combined.lower()):
            analysis["failure_type"] = "test_script_error"
            analysis["is_code_bug"] = False
            analysis["recommendation"] = "modify_test"
            analysis["details"].append("测试脚本对LLM回复内容的断言失败，可能是测试脚本问题")
        
        # 检查测试脚本断言
        elif "test_" in combined and ("assert" in combined.lower() or "expect" in combined.lower()):
            analysis["failure_type"] = "test_script_error"
            analysis["is_code_bug"] = False
            analysis["recommendation"] = "analyze_test"
            analysis["details"].append("测试脚本断言失败，需要分析是代码问题还是测试期望问题")
        
        return analysis
    
    def run_repair_cycle(
        self,
        original_request: str,
        initial_operations: List[Dict[str, Any]],
        version_manager,
        test_runner,
        decision: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        运行完整的修复周期
        
        Args:
            original_request: 原始请求
            initial_operations: 初始操作
            version_manager: 版本管理器
            test_runner: 测试运行器
            decision: 决策结果，包含是否需要运行测试等信息
            
        Returns:
            dict: 执行结果
        """
        # 1. 创建candidate
        print("[SelfRepair] 正在创建candidate工作区...")
        create_result = version_manager.create_candidate()
        if not create_result["success"]:
            print(f"[SelfRepair] 创建candidate失败: {create_result['message']}")
            return {
                "success": False,
                "message": f"创建candidate失败: {create_result['message']}"
            }
        print("[SelfRepair] candidate工作区创建成功")
        
        # 2. 应用初始操作
        print(f"[SelfRepair] 正在应用初始操作，共 {len(initial_operations)} 个操作...")
        apply_result = version_manager.apply_operations_to_candidate(initial_operations)
        if not apply_result["success"]:
            print(f"[SelfRepair] 应用操作失败: {apply_result['message']}")
            version_manager.rollback_candidate()
            return {
                "success": False,
                "message": f"应用操作失败: {apply_result['message']}"
            }
        print("[SelfRepair] 初始操作应用成功")
        
        # 3. 根据决策决定是否运行测试
        tests_to_run = decision.get("tests_to_run", []) if decision else []
        run_tests = decision.get("run_tests", True) if decision else True  # 默认运行测试
        
        if run_tests and tests_to_run:
            print(f"[SelfRepair] 正在运行指定测试: {tests_to_run}...")
            # 运行指定的测试
            test_result = self._run_specific_tests(version_manager, test_runner, tests_to_run)
        elif run_tests:
            print("[SelfRepair] 正在运行测试...")
            # 智能选择测试：如果有修改的文件，尝试运行相关测试
            related_tests = self._find_related_tests(initial_operations)
            if related_tests:
                print(f"[SelfRepair] 发现相关测试: {related_tests}")
                test_result = self._run_specific_tests(version_manager, test_runner, related_tests)
            else:
                # 没有找到相关测试，运行默认测试
                print("[SelfRepair] 运行默认测试...")
                test_result = version_manager.run_candidate_tests()
        else:
            print("[SelfRepair] 根据决策，跳过测试运行")
            # 不运行测试，直接提升candidate
            test_result = {"success": True, "message": "跳过测试运行"}
        
        if test_result["success"]:
            print("[SelfRepair] 测试通过，正在提升candidate...")
            # 测试通过，提升candidate
            promote_result = version_manager.promote_candidate()
            return {
                "success": True,
                "message": "操作成功，测试通过，已提升candidate",
                "test_result": test_result,
                "promote_result": promote_result
            }
        
        # 4. 测试失败，分析失败原因
        print("[SelfRepair] 测试失败，正在分析失败原因...")
        failure_analysis = self._analyze_test_failures(test_result)
        print(f"[SelfRepair] 失败分析: {failure_analysis['failure_type']}")
        print(f"[SelfRepair] 是否代码bug: {failure_analysis['is_code_bug']}")
        
        # 如果分析认为不是代码bug，可以考虑跳过修复
        if not failure_analysis["is_code_bug"]:
            print("[SelfRepair] 分析认为这不是代码bug，可能是测试脚本问题")
            # 尝试让LLM决定是否需要修复
            # 不直接跳过，让LLM判断
        
        # 5. 尝试修复
        print("[SelfRepair] 正在尝试修复...")
        for attempt in range(self.max_repair_attempts):
            print(f"[SelfRepair] 修复尝试 {attempt + 1}/{self.max_repair_attempts}")
            # 获取失败日志
            error_logs = test_result.get("stderr", "") + "\n" + test_result.get("stdout", "")
            
            # 获取已修改的文件内容
            changed_files = {}
            for op in initial_operations:
                if "path" in op:
                    try:
                        file_path = version_manager.candidate_dir / op["path"]
                        if file_path.exists():
                            with open(file_path, 'r', encoding='utf-8') as f:
                                changed_files[op["path"]] = f.read()
                    except Exception:
                        pass
            
            # 请求LLM修复
            print("[SelfRepair] 正在请求LLM生成修复方案...")
            repair_result = self.attempt_repair(
                original_request,
                initial_operations,
                error_logs,
                changed_files,
                failure_analysis
            )
            
            if not repair_result["success"]:
                print("[SelfRepair] LLM无法生成修复方案")
                continue
            
            # 验证并过滤修复操作
            repair_operations = repair_result.get("repair_operations", [])
            valid_operations = self.update_executor.validate_operations(repair_operations)
            
            if not valid_operations:
                print("[SelfRepair] 没有有效的修复操作")
                continue
            
            print(f"[SelfRepair] 正在应用修复操作，共 {len(valid_operations)} 个有效操作...")
            repair_apply_result = version_manager.apply_operations_to_candidate(valid_operations)
            
            if not repair_apply_result["success"]:
                print("[SelfRepair] 应用修复操作失败")
                continue
            
            # 重新测试
            if run_tests and tests_to_run:
                print(f"[SelfRepair] 正在重新运行测试: {tests_to_run}...")
                test_result = self._run_specific_tests(version_manager, test_runner, tests_to_run)
            elif run_tests:
                print("[SelfRepair] 正在重新运行测试...")
                # 使用智能测试选择
                related_tests = self._find_related_tests(valid_operations)
                if related_tests:
                    print(f"[SelfRepair] 发现相关测试: {related_tests}")
                    test_result = self._run_specific_tests(version_manager, test_runner, related_tests)
                else:
                    test_result = version_manager.run_candidate_tests()
            else:
                print("[SelfRepair] 根据决策，跳过测试运行")
                test_result = {"success": True, "message": "跳过测试运行"}
            
            if test_result["success"]:
                print("[SelfRepair] 修复成功，正在提升candidate...")
                # 修复成功，提升candidate
                promote_result = version_manager.promote_candidate()
                return {
                    "success": True,
                    "message": f"经过{attempt + 1}次修复后成功",
                    "test_result": test_result,
                    "promote_result": promote_result,
                    "repair_attempts": attempt + 1,
                    "failure_analysis": failure_analysis
                }
        
        # 6. 修复失败，回滚
        version_manager.rollback_candidate()
        
        return {
            "success": False,
            "message": f"经过{self.max_repair_attempts}次修复尝试仍失败，已回滚",
            "test_result": test_result,
            "failure_analysis": failure_analysis
        }
    
    def _run_specific_tests(self, version_manager, test_runner, tests_to_run: List[str]) -> Dict[str, Any]:
        """
        运行指定的测试
        
        Args:
            version_manager: 版本管理器
            test_runner: 测试运行器
            tests_to_run: 要运行的测试列表
            
        Returns:
            dict: 测试结果
        """
        # 构建测试命令
        test_cmd = ["python3", "-m", "pytest"] + tests_to_run + ["-v"]
        
        # 使用version_manager运行测试
        return version_manager.run_candidate_tests(test_cmd=test_cmd)
    
    def _find_related_tests(self, operations: List[Dict[str, Any]]) -> List[str]:
        """
        根据操作列表查找相关的测试文件
        
        Args:
            operations: 操作列表
            
        Returns:
            list: 相关测试文件路径列表
        """
        test_files = []
        
        for op in operations:
            path = op.get("path", "")
            if not path:
                continue
            
            # 跳过测试文件本身
            if "test_" in path or path.startswith("tests/"):
                continue
            
            # 根据修改的文件推断测试文件
            if path.startswith("core/"):
                # 核心模块的测试
                module_name = path.replace("core/", "").replace(".py", "")
                test_file = f"tests/test_{module_name}.py"
                if test_file not in test_files:
                    test_files.append(test_file)
            elif path.startswith("kernel/"):
                # 内核模块的测试
                module_name = path.replace("kernel/", "").replace(".py", "")
                test_file = f"tests/test_{module_name}.py"
                if test_file not in test_files:
                    test_files.append(test_file)
            elif path.startswith("mind/"):
                # 心智模块的测试
                module_name = path.replace("mind/", "").replace(".py", "")
                test_file = f"tests/test_{module_name}.py"
                if test_file not in test_files:
                    test_files.append(test_file)
        
        # 如果没有找到相关测试，尝试查找通用测试
        if not test_files:
            # 检查是否有通用测试文件
            import os
            tests_dir = "tests"
            if os.path.exists(tests_dir):
                for f in os.listdir(tests_dir):
                    if f.startswith("test_") and f.endswith(".py"):
                        test_files.append(os.path.join(tests_dir, f))
                        break  # 只取第一个
        
        return test_files


# 全局实例
_self_repair_loop = None


def get_self_repair_loop() -> SelfRepairLoop:
    """获取全局自修复循环实例"""
    global _self_repair_loop
    if _self_repair_loop is None:
        from core.llm_client import get_llm_client
        from core.decision_engine import get_decision_engine
        from core.update_executor import get_update_executor
        _self_repair_loop = SelfRepairLoop(
            get_llm_client(), 
            get_decision_engine(), 
            get_update_executor()
        )
    return _self_repair_loop
