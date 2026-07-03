"""
CLI交互测试

测试覆盖：
1. 普通文件操作（偏好写入、listen policy 更新）
2. 多轮决策（read_more_files -> 最终决策）
3. candidate 工作区流程（创建 -> 应用 -> 提升/回滚）
4. 回滚机制验证
"""

import sys
import os
import threading
import ctypes
import json
import tempfile
import shutil
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 加载.env文件
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()


def _raise_in_thread(thread, exc_type):
    """在指定线程中抛出异常"""
    tid = thread.ident
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid),
        ctypes.py_object(exc_type)
    )
    if res > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)


def run_with_timeout(func, timeout_seconds=30):
    """使用threading运行函数，带超时控制"""
    result = [None]
    error = [None]

    def target():
        try:
            result[0] = func()
        except Exception as e:
            error[0] = e

    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        _raise_in_thread(thread, SystemExit)
        thread.join(2)
        return False, f"操作超时（{timeout_seconds}秒）"

    if error[0] is not None:
        return False, str(error[0])

    return True, result[0]


# ============================================================
# 测试1：普通文件操作 - 咖啡偏好写入
# ============================================================
def test_scenario_1():
    """测试场景1：咖啡偏好写入"""
    # 创建临时目录
    test_dir = tempfile.mkdtemp()
    project_root = Path(test_dir)
    
    try:
        def do_test():
            from core.agent_loop import AgentLoop
            agent = AgentLoop(project_root)
            response = agent.process_input("我不爱喝瑞幸，我爱喝星巴克。")
            coffee_file = Path(agent.project_root) / "memory/preferences/coffee.md"
            content = coffee_file.read_text() if coffee_file.exists() else ""
            return {
                "response": response,
                "file_exists": coffee_file.exists(),
                "contains_keyword": "星巴克" in content
            }

        print("=" * 60)
        print("测试1：咖啡偏好写入")
        print("输入：我不爱喝瑞幸，我爱喝星巴克。")
        print("-" * 60)

        try:
            success, result = run_with_timeout(do_test, timeout_seconds=60)
            if success:
                print(f"回复：{result['response']}")
                if result['file_exists'] and result['contains_keyword']:
                    print("✓ coffee.md 包含'星巴克'")
                else:
                    print(f"✗ 验证失败: exists={result['file_exists']}, keyword={result['contains_keyword']}")
            else:
                print(f"✗ {result}")
        except Exception as e:
            print(f"✗ 错误: {e}")
        print()
    finally:
        # 清理临时目录
        shutil.rmtree(test_dir, ignore_errors=True)


# ============================================================
# 测试2：listen policy 更新
# ============================================================
def test_scenario_2():
    """测试场景2：listen policy 更新"""
    # 创建临时目录
    test_dir = tempfile.mkdtemp()
    project_root = Path(test_dir)
    
    try:
        def do_test():
            from core.agent_loop import AgentLoop
            agent = AgentLoop(project_root)
            response = agent.process_input("以后我叫你大李的时候你才应，和别人聊天时别插嘴。")
            listen_policy_file = Path(agent.project_root) / "mind/listen_policy.md"
            content = listen_policy_file.read_text() if listen_policy_file.exists() else ""
            return {
                "response": response,
                "contains_keyword": "大李" in content
            }

        print("=" * 60)
        print("测试2：listen policy 更新")
        print("输入：以后我叫你大李的时候你才应，和别人聊天时别插嘴。")
        print("-" * 60)

        try:
            success, result = run_with_timeout(do_test, timeout_seconds=60)
            if success:
                print(f"回复：{result['response']}")
                if result['contains_keyword']:
                    print("✓ listen_policy.md 包含'大李'")
                else:
                    print(f"✗ 验证失败: keyword={result['contains_keyword']}")
            else:
                print(f"✗ {result}")
        except Exception as e:
            print(f"✗ 错误: {e}")
        print()
    finally:
        # 清理临时目录
        shutil.rmtree(test_dir, ignore_errors=True)


# ============================================================
# 测试3：candidate 工作区流程验证
# ============================================================
def test_scenario_3():
    """测试场景3：candidate 工作区流程（创建 -> 应用 -> 提升）"""
    # 创建临时目录
    test_dir = tempfile.mkdtemp()
    project_root = Path(test_dir)
    
    try:
        def do_test():
            from kernel.version_manager import VersionManager

            vm = VersionManager(str(project_root))

            results = {}

            # 1. 创建 candidate
            create_result = vm.create_candidate()
            results["create_success"] = create_result["success"]
            results["candidate_exists"] = vm.candidate_dir.exists()

            if not create_result["success"]:
                return results

            # 2. 在 candidate 中应用一个安全的操作
            test_content = "\n\n## [测试] candidate 流程测试\n\n- 测试时间: 2026-07-02\n"
            apply_result = vm.apply_operations_to_candidate([
                {
                    "type": "append_file",
                    "path": "self_development/issues.md",
                    "content": test_content
                }
            ])
            results["apply_success"] = apply_result.get("success", False)

            # 3. 验证操作已应用到 candidate
            candidate_issues = vm.candidate_dir / "self_development" / "issues.md"
            if candidate_issues.exists():
                candidate_content = candidate_issues.read_text()
                results["operation_applied"] = "candidate 流程测试" in candidate_content
            else:
                results["operation_applied"] = False

            # 4. 提升 candidate（跳过测试，直接提升）
            promote_result = vm.promote_candidate()
            results["promote_success"] = promote_result["success"]
            results["candidate_cleaned"] = not vm.candidate_dir.exists()

            # 5. 验证操作已应用到主项目
            main_issues = project_root / "self_development" / "issues.md"
            if main_issues.exists():
                main_content = main_issues.read_text()
                results["main_updated"] = "candidate 流程测试" in main_content
            else:
                results["main_updated"] = False

            return results

        print("=" * 60)
        print("测试3：candidate 工作区流程验证")
        print("-" * 60)

        try:
            success, result = run_with_timeout(do_test, timeout_seconds=60)
            if success:
                print(f"创建 candidate: {'✓' if result.get('create_success') else '✗'}")
                print(f"candidate 存在: {'✓' if result.get('candidate_exists') else '✗'}")
                print(f"应用操作: {'✓' if result.get('apply_success') else '✗'}")
                print(f"操作已应用到 candidate: {'✓' if result.get('operation_applied') else '✗'}")
                print(f"提升 candidate: {'✓' if result.get('promote_success') else '✗'}")
                print(f"candidate 已清理: {'✓' if result.get('candidate_cleaned') else '✗'}")
                print(f"主项目已更新: {'✓' if result.get('main_updated') else '✗'}")

                all_pass = all([
                    result.get('create_success'),
                    result.get('candidate_exists'),
                    result.get('apply_success'),
                    result.get('operation_applied'),
                    result.get('promote_success'),
                    result.get('candidate_cleaned'),
                    result.get('main_updated')
                ])
                if all_pass:
                    print("\n✓ candidate 工作区流程完整验证通过")
                else:
                    print("\n✗ candidate 工作区流程验证失败")
            else:
                print(f"✗ {result}")
        except Exception as e:
            print(f"✗ 错误: {e}")
        print()
    finally:
        # 清理临时目录
        shutil.rmtree(test_dir, ignore_errors=True)


# ============================================================
# 测试4：回滚机制验证
# ============================================================
def test_scenario_4():
    """测试场景4：回滚机制验证"""
    # 创建临时目录
    test_dir = tempfile.mkdtemp()
    project_root = Path(test_dir)
    
    try:
        def do_test():
            from kernel.version_manager import VersionManager

            vm = VersionManager(str(project_root))

            results = {}

            # 1. 创建 candidate
            create_result = vm.create_candidate()
            results["create_success"] = create_result["success"]

            if not create_result["success"]:
                return results

            # 2. 在 candidate 中应用一个操作
            test_content = "\n\n## [回滚测试] 这个内容应该被回滚\n\n- 回滚测试标记\n"
            apply_result = vm.apply_operations_to_candidate([
                {
                    "type": "append_file",
                    "path": "self_development/issues.md",
                    "content": test_content
                }
            ])
            results["apply_success"] = apply_result.get("success", False)

            # 3. 回滚 candidate
            rollback_result = vm.rollback_candidate()
            results["rollback_success"] = rollback_result["success"]
            results["candidate_cleaned"] = not vm.candidate_dir.exists()

            # 4. 验证主项目未被修改
            main_issues = project_root / "self_development" / "issues.md"
            if main_issues.exists():
                main_content = main_issues.read_text()
                results["main_not_modified"] = "回滚测试" not in main_content
            else:
                results["main_not_modified"] = True

            return results

        print("=" * 60)
        print("测试4：回滚机制验证")
        print("-" * 60)

        try:
            success, result = run_with_timeout(do_test, timeout_seconds=60)
            if success:
                print(f"创建 candidate: {'✓' if result.get('create_success') else '✗'}")
                print(f"应用操作: {'✓' if result.get('apply_success') else '✗'}")
                print(f"回滚成功: {'✓' if result.get('rollback_success') else '✗'}")
                print(f"candidate 已清理: {'✓' if result.get('candidate_cleaned') else '✗'}")
                print(f"主项目未修改: {'✓' if result.get('main_not_modified') else '✗'}")

                all_pass = all([
                    result.get('create_success'),
                    result.get('apply_success'),
                    result.get('rollback_success'),
                    result.get('candidate_cleaned'),
                    result.get('main_not_modified')
                ])
                if all_pass:
                    print("\n✓ 回滚机制验证通过")
                else:
                    print("\n✗ 回滚机制验证失败")
            else:
                print(f"✗ {result}")
        except Exception as e:
            print(f"✗ 错误: {e}")
        print()
    finally:
        # 清理临时目录
        shutil.rmtree(test_dir, ignore_errors=True)


# ============================================================
# 测试5：LLM 多轮决策验证
# ============================================================
def test_scenario_5():
    """测试场景5：LLM 多轮决策（read_more_files -> 最终决策）"""
    # 创建临时目录
    test_dir = tempfile.mkdtemp()
    project_root = Path(test_dir)
    
    try:
        def do_test():
            from core.decision_engine import DecisionEngine
            from core.context_builder import ContextBuilder
            from core.project_reader import ProjectReader
            from core.llm_client import get_llm_client

            llm_client = get_llm_client()
            project_reader = ProjectReader(str(project_root))
            context_builder = ContextBuilder(project_reader)
            decision_engine = DecisionEngine(llm_client, context_builder)

            # 第一次决策
            first_decision = decision_engine.make_decision("你给记忆加一个 hit 机制。")

            results = {
                "first_decision": {
                    "request_type": first_decision.get("request_type"),
                    "needs_code_change": first_decision.get("needs_code_change"),
                    "has_read_more_files": bool(first_decision.get("read_more_files")),
                    "operations_count": len(first_decision.get("operations", []))
                }
            }

            # 如果需要读取更多文件，进行二次决策
            if first_decision.get("read_more_files"):
                more_files_content = project_reader.read_files(first_decision["read_more_files"])
                final_decision = decision_engine.make_decision_with_more_files(
                    "你给记忆加一个 hit 机制。", first_decision, more_files_content
                )
                results["final_decision"] = {
                    "request_type": final_decision.get("request_type"),
                    "needs_code_change": final_decision.get("needs_code_change"),
                    "operations_count": len(final_decision.get("operations", []))
                }
                results["has_final_decision"] = True
            else:
                results["has_final_decision"] = False

            return results

        print("=" * 60)
        print("测试5：LLM 多轮决策验证")
        print("输入：你给记忆加一个 hit 机制。")
        print("-" * 60)

        try:
            success, result = run_with_timeout(do_test, timeout_seconds=120)
            if success:
                print(f"\n--- 第一次决策 ---")
                fd = result.get('first_decision', {})
                print(f"  request_type: {fd.get('request_type')}")
                print(f"  needs_code_change: {fd.get('needs_code_change')}")
                print(f"  has_read_more_files: {fd.get('has_read_more_files')}")
                print(f"  operations_count: {fd.get('operations_count')}")

                if result.get('has_final_decision'):
                    print(f"\n--- 最终决策 ---")
                    fnd = result.get('final_decision', {})
                    print(f"  request_type: {fnd.get('request_type')}")
                    print(f"  needs_code_change: {fnd.get('needs_code_change')}")
                    print(f"  operations_count: {fnd.get('operations_count')}")

                if fd.get('has_read_more_files') and result.get('has_final_decision'):
                    print("\n✓ 多轮决策流程正常")
                elif not fd.get('has_read_more_files'):
                    print("\n✓ 单轮决策流程正常")
                else:
                    print("\n✗ 多轮决策流程异常")
            else:
                print(f"✗ {result}")
        except Exception as e:
            print(f"✗ 错误: {e}")
        print()
    finally:
        # 清理临时目录
        shutil.rmtree(test_dir, ignore_errors=True)


# ============================================================
# 测试6：健康检查
# ============================================================
def test_health_check():
    """测试健康检查"""
    # 创建临时目录
    test_dir = tempfile.mkdtemp()
    project_root = Path(test_dir)
    
    try:
        print("=" * 60)
        print("测试6：健康检查")
        print("-" * 60)

        def do_test():
            from core.agent_loop import AgentLoop
            agent = AgentLoop(project_root)
            return agent.run_health_check()

        try:
            success, result = run_with_timeout(do_test, timeout_seconds=30)
            if success:
                print(f"健康检查: {'通过' if result['pass'] else '失败'}")
                if result.get('reasons'):
                    for reason in result['reasons']:
                        print(f"  - {reason}")
            else:
                print(f"✗ {result}")
        except Exception as e:
            print(f"✗ 错误: {e}")
        print()
    finally:
        # 清理临时目录
        shutil.rmtree(test_dir, ignore_errors=True)


# ============================================================
# 主函数
# ============================================================
if __name__ == "__main__":
    test_health_check()
    test_scenario_1()
    test_scenario_2()
    test_scenario_3()  # candidate 工作区流程
    test_scenario_4()  # 回滚机制
    test_scenario_5()  # 多轮决策

    print("=" * 60)
    print("=== 所有测试完成 ===")
    print("=" * 60)
