#!/usr/bin/env python3
"""
测试 ReAct Agent
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from core.llm_client import LLMClient
from core.react_agent import ReActAgent
from core.builtin_tools import register_builtin_tools


def test_react_agent():
    """测试 ReAct Agent"""
    print("=" * 60)
    print("测试 ReAct Agent")
    print("=" * 60)

    # 1. 注册内置工具
    print("\n1. 注册内置工具...")
    register_builtin_tools()

    # 2. 创建 LLM 客户端
    print("\n2. 创建 LLM 客户端...")
    try:
        llm_client = LLMClient()
        print("   LLM 客户端创建成功")
    except Exception as e:
        print(f"   LLM 客户端创建失败: {e}")
        print("   跳过测试")
        return

    # 3. 创建 ReAct Agent
    print("\n3. 创建 ReAct Agent...")
    agent = ReActAgent(
        llm_client=llm_client,
        max_iterations=5,
        message_callback=lambda msg: print(f"   {msg}")
    )
    print("   ReAct Agent 创建成功")

    # 4. 测试简单任务
    print("\n4. 测试简单任务: '列出当前目录的文件'")
    print("-" * 60)
    result = agent.run("列出当前目录的文件")
    print("-" * 60)
    print(f"结果: {result}")

    # 5. 测试读取文件
    print("\n5. 测试读取文件: '读取 README.md 的内容'")
    print("-" * 60)
    result = agent.run("读取 README.md 的内容")
    print("-" * 60)
    print(f"结果: {result}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_react_agent()
