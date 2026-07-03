"""
会话记忆管理器测试

测试ConversationSessionManager的核心功能。
"""

import os
import sys
import json
import shutil
import tempfile
from pathlib import Path
from datetime import datetime

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.conversation_session_manager import ConversationSessionManager


def test_conversation_session_manager():
    """测试会话记忆管理器"""
    print("=== 测试会话记忆管理器 ===")
    
    # 创建临时目录
    temp_dir = tempfile.mkdtemp()
    
    try:
        # 初始化会话管理器
        session_manager = ConversationSessionManager(temp_dir, max_recent_messages=3)
        
        # 测试1: 创建新线程
        print("\n1. 测试创建新线程...")
        thread = session_manager.create_thread("测试用户请求", "chat")
        assert thread["thread_id"] is not None
        assert thread["status"] == "running"
        assert thread["task_type"] == "chat"
        assert thread["user_request"] == "测试用户请求"
        print("✓ 创建线程成功")
        
        thread_id = thread["thread_id"]
        
        # 测试2: 追加消息
        print("\n2. 测试追加消息...")
        session_manager.append_message(thread_id, "user", "用户消息1")
        session_manager.append_message(thread_id, "assistant", "助手回复1")
        session_manager.append_message(thread_id, "user", "用户消息2")
        
        # 重新加载线程（create_thread时已有1条user消息，加上3条共4条）
        thread = session_manager.load_thread(thread_id)
        assert len(thread["messages"]) == 4
        print("✓ 追加消息成功")
        
        # 测试3: 更新目标
        print("\n3. 测试更新目标...")
        session_manager.update_goal(thread_id, "测试目标")
        thread = session_manager.load_thread(thread_id)
        assert thread["current_goal"] == "测试目标"
        print("✓ 更新目标成功")
        
        # 测试4: 记录读取的文件
        print("\n4. 测试记录读取文件...")
        session_manager.add_read_file(thread_id, "test/file.py", "测试读取")
        thread = session_manager.load_thread(thread_id)
        assert len(thread["read_files"]) == 1
        assert thread["read_files"][0]["path"] == "test/file.py"
        print("✓ 记录读取文件成功")
        
        # 测试5: 记录修改的文件
        print("\n5. 测试记录修改文件...")
        session_manager.add_modified_file(thread_id, "test/file.py", "测试修改")
        thread = session_manager.load_thread(thread_id)
        assert len(thread["modified_files"]) == 1
        assert thread["modified_files"][0]["path"] == "test/file.py"
        print("✓ 记录修改文件成功")
        
        # 测试6: 记录工具结果
        print("\n6. 测试记录工具结果...")
        session_manager.add_tool_result(thread_id, "read_file", "test/file.py", "读取成功")
        thread = session_manager.load_thread(thread_id)
        assert len(thread["tool_results"]) == 1
        assert thread["tool_results"][0]["tool"] == "read_file"
        print("✓ 记录工具结果成功")
        
        # 测试7: 记录测试结果
        print("\n7. 测试记录测试结果...")
        session_manager.add_test_result(thread_id, "pytest", True, "测试通过")
        thread = session_manager.load_thread(thread_id)
        assert len(thread["test_results"]) == 1
        assert thread["test_results"][0]["passed"] == True
        print("✓ 记录测试结果成功")
        
        # 测试8: 更新工作摘要
        print("\n8. 测试更新工作摘要...")
        session_manager.update_working_summary(thread_id)
        thread = session_manager.load_thread(thread_id)
        assert thread["working_summary"] != ""
        print("✓ 更新工作摘要成功")
        
        # 测试9: 更新最后决策
        print("\n9. 测试更新最后决策...")
        decision = {"request_type": "chat", "summary": "测试决策"}
        session_manager.update_last_decision(thread_id, decision)
        thread = session_manager.load_thread(thread_id)
        assert thread["last_llm_decision"] == decision
        print("✓ 更新最后决策成功")
        
        # 测试10: 更新状态
        print("\n10. 测试更新状态...")
        session_manager.update_status(thread_id, "completed")
        thread = session_manager.load_thread(thread_id)
        assert thread["status"] == "completed"
        print("✓ 更新状态成功")
        
        # 测试11: 获取上下文
        print("\n11. 测试获取上下文...")
        context = session_manager.get_context_for_llm(thread_id)
        assert context["thread_id"] == thread_id
        assert context["task_type"] == "chat"
        assert context["status"] == "completed"
        print("✓ 获取上下文成功")
        
        # 测试12: 格式化上下文
        print("\n12. 测试格式化上下文...")
        formatted = session_manager.format_thread_context_for_llm(thread_id)
        assert "当前任务状态" in formatted
        assert "工作摘要" in formatted
        assert "最近对话" in formatted
        print("✓ 格式化上下文成功")
        
        # 测试13: 消息压缩
        print("\n13. 测试消息压缩...")
        # 添加更多消息触发压缩
        for i in range(10):
            session_manager.append_message(thread_id, "user", f"消息{i}")
            session_manager.append_message(thread_id, "assistant", f"回复{i}")
        
        thread = session_manager.load_thread(thread_id)
        # 消息应该被压缩
        assert len(thread["messages"]) <= 6  # max_recent_messages * 2
        print("✓ 消息压缩成功")
        
        # 测试14: 归档线程
        print("\n14. 测试归档线程...")
        session_manager.archive_thread(thread_id)
        
        # 检查是否归档成功
        archived_path = Path(temp_dir) / "runtime" / "threads" / "archived" / f"{thread_id}.json"
        assert archived_path.exists()
        print("✓ 归档线程成功")
        
        print("\n=== 所有测试通过！ ===")
        return True
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # 清理临时目录
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_thread_lifecycle():
    """测试线程生命周期"""
    print("\n=== 测试线程生命周期 ===")
    
    temp_dir = tempfile.mkdtemp()
    
    try:
        session_manager = ConversationSessionManager(temp_dir)
        
        # 创建新线程
        thread1 = session_manager.create_thread("任务1", "chat")
        thread1_id = thread1["thread_id"]
        
        # 获取活跃线程
        active = session_manager.get_active_thread()
        assert active is not None
        assert active["thread_id"] == thread1_id
        
        # 完成任务1
        session_manager.update_status(thread1_id, "completed")
        
        # 创建新线程（应该自动归档旧线程）
        thread2 = session_manager.create_thread("任务2", "self_development")
        thread2_id = thread2["thread_id"]
        
        # 检查活跃线程已更新
        active = session_manager.get_active_thread()
        assert active is not None
        assert active["thread_id"] == thread2_id
        
        # 检查旧线程已归档
        archived_path = Path(temp_dir) / "runtime" / "threads" / "archived" / f"{thread1_id}.json"
        assert archived_path.exists()
        
        print("✓ 线程生命周期测试通过")
        return True
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    print("开始测试会话记忆管理器...")
    
    success1 = test_conversation_session_manager()
    success2 = test_thread_lifecycle()
    
    if success1 and success2:
        print("\n🎉 所有测试通过！")
        sys.exit(0)
    else:
        print("\n💥 测试失败！")
        sys.exit(1)
