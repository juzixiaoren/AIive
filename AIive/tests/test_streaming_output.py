"""
流式输出测试

测试LLM客户端的流式输出功能。
"""

import sys
import os
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


def test_llm_client_streaming():
    """测试LLM客户端流式输出"""
    print("=" * 60)
    print("测试：LLM客户端流式输出")
    print("-" * 60)
    
    try:
        from core.llm_client import LLMClient
        
        llm_client = LLMClient()
        
        messages = [
            {"role": "user", "content": "请用中文简单介绍一下你自己，不超过50个字。"}
        ]
        
        print("开始流式输出测试...")
        print("Agent: ", end="", flush=True)
        
        full_response = ""
        chunk_count = 0
        
        for chunk in llm_client.chat_stream(messages, temperature=0.7):
            print(chunk, end="", flush=True)
            full_response += chunk
            chunk_count += 1
        
        print()  # 换行
        
        print(f"\n流式输出完成")
        print(f"接收到的chunk数量: {chunk_count}")
        print(f"完整回复长度: {len(full_response)} 字符")
        print(f"完整回复内容: {full_response[:100]}...")
        
        if chunk_count > 0 and len(full_response) > 0:
            print("✓ 流式输出测试通过")
            return True
        else:
            print("✗ 流式输出测试失败：没有接收到任何内容")
            return False
            
    except Exception as e:
        print(f"✗ 流式输出测试失败：{e}")
        return False


def test_agent_loop_streaming():
    """测试Agent主循环的流式输出"""
    print("\n" + "=" * 60)
    print("测试：Agent主循环流式输出")
    print("-" * 60)
    
    # 创建临时目录
    test_dir = tempfile.mkdtemp()
    project_root = Path(test_dir)
    
    try:
        from core.agent_loop import AgentLoop
        
        agent = AgentLoop(project_root)
        
        print("测试聊天流式输出...")
        response = agent.process_input("你好，我是测试用户。", stream=True)
        
        print(f"\n完整回复长度: {len(response)} 字符")
        print(f"回复内容: {response[:100]}...")
        
        if len(response) > 0:
            print("✓ Agent主循环流式输出测试通过")
            return True
        else:
            print("✗ Agent主循环流式输出测试失败：回复为空")
            return False
            
    except Exception as e:
        print(f"✗ Agent主循环流式输出测试失败：{e}")
        return False
    finally:
        # 清理临时目录
        shutil.rmtree(test_dir, ignore_errors=True)


def test_non_streaming_fallback():
    """测试非流式回退"""
    print("\n" + "=" * 60)
    print("测试：非流式回退")
    print("-" * 60)
    
    # 创建临时目录
    test_dir = tempfile.mkdtemp()
    project_root = Path(test_dir)
    
    try:
        from core.agent_loop import AgentLoop
        
        agent = AgentLoop(project_root)
        
        print("测试非流式输出...")
        response = agent.process_input("简单回复'OK'即可。", stream=False)
        
        print(f"回复内容: {response}")
        
        if response and len(response) > 0:
            print("✓ 非流式回退测试通过")
            return True
        else:
            print("✗ 非流式回退测试失败：回复为空")
            return False
            
    except Exception as e:
        print(f"✗ 非流式回退测试失败：{e}")
        return False
    finally:
        # 清理临时目录
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    print("开始流式输出测试套件")
    print("=" * 60)
    
    results = []
    
    # 测试1：LLM客户端流式输出
    results.append(test_llm_client_streaming())
    
    # 测试2：Agent主循环流式输出
    results.append(test_agent_loop_streaming())
    
    # 测试3：非流式回退
    results.append(test_non_streaming_fallback())
    
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    
    test_names = [
        "LLM客户端流式输出",
        "Agent主循环流式输出",
        "非流式回退"
    ]
    
    for i, (name, result) in enumerate(zip(test_names, results)):
        print(f"{i+1}. {name}: {'✓ 通过' if result else '✗ 失败'}")
    
    passed = sum(results)
    total = len(results)
    
    print(f"\n总计: {passed}/{total} 测试通过")
    
    if passed == total:
        print("🎉 所有流式输出测试通过！")
    else:
        print("⚠️  部分测试失败，请检查实现")