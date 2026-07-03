#!/usr/bin/env python3
"""调试场景3的LLM决策"""

import sys
import os
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# 加载.env文件
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()

from core.llm_client import get_llm_client
from core.project_reader import ProjectReader
from core.context_builder import ContextBuilder
from core.decision_engine import DecisionEngine

# 创建组件
project_reader = ProjectReader()
llm_client = get_llm_client()
context_builder = ContextBuilder(project_reader)
decision_engine = DecisionEngine(llm_client, context_builder)

# 测试决策
user_input = "你给记忆加一个 hit 机制。"
print(f"用户输入: {user_input}\n")

decision = decision_engine.make_decision(user_input)
print(f"决策结果:")
print(json.dumps(decision, indent=2, ensure_ascii=False))

print(f"\nneeds_code_change: {decision.get('needs_code_change')}")
print(f"operations数量: {len(decision.get('operations', []))}")
print(f"request_type: {decision.get('request_type')}")
